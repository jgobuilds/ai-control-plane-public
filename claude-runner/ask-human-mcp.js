#!/usr/bin/env node
/**
 * ask-human — an MCP tool that lets the agent STOP MID-RUN and ask a person.
 *
 * WHY THIS EXISTS. The control plane could halt an agent (kill switch) and could
 * approve an action before it happened (approval gate). Neither let the agent say
 * "I do not know this, and guessing would be wrong." So it guessed. This is the
 * missing state: the run pauses inside a tool call, a human is paged, and the
 * answer comes back into the SAME conversation with its context intact.
 *
 * WHY MCP RATHER THAN A SENTINEL IN THE OUTPUT. An agent that ends its turn with
 * "I need X" has already lost the reasoning that got it there; resuming means
 * re-prompting and hoping. A tool call blocks in place — the model is still
 * mid-thought when the answer arrives.
 *
 * NETWORK. This is the ONE outbound call the runner makes to the control plane,
 * over tcp/5678 to the docker network — a hole init-firewall.sh already opens for
 * n8n-mcp. No widening of the containment boundary was needed, and none was done.
 *
 * BOUNDED BY DESIGN. The question gate supports a 24h wait; an HTTP request
 * cannot. The runner's exec timeout is 15 minutes, so this caps at ~10 and
 * returns UNANSWERED rather than pretending. Genuinely long waits need durable
 * execution (docs/decisions/0010), and faking them here would be worse than not
 * having them.
 *
 * Stdio JSON-RPC, no dependencies — it runs inside a hardened image where adding
 * a package tree is a supply-chain decision, not a convenience.
 */
"use strict";

const crypto = require("node:crypto");

const ENDPOINT = process.env.ASK_HUMAN_URL || "http://n8n:5678/webhook/ask-human";
const TOKEN = process.env.RUNNER_TOKEN || "";
// Where the answer is read back from: this runner's OWN server, over loopback.
// The answer is pushed in by the router (the runner has no egress to it, by
// design) and parked in an in-memory mailbox. Loopback is the one destination
// the firewall accepts unconditionally, so no rule changed to make this work.
const STATUS_URL = process.env.ASK_HUMAN_STATUS_URL || "http://127.0.0.1:8080/ask/status";
const POLL_MS = Number(process.env.ASK_HUMAN_POLL_MS) || 3000;
const TIMEOUT_MS = Math.min(
  Number(process.env.ASK_HUMAN_TIMEOUT_MS) || 10 * 60 * 1000,
  14 * 60 * 1000 // stay under the runner's own 15-minute exec timeout
);

const TOOL = {
  name: "ask_human",
  description:
    "Ask a human operator a question and WAIT for their answer. Use this when a " +
    "fact you need is genuinely unavailable to you — a folder id, which client a " +
    "file belongs to, whether to proceed with an ambiguous instruction — and " +
    "guessing would produce confidently wrong work. Blocks for up to 10 minutes. " +
    "If nobody answers you will get answered=false; treat that as UNKNOWN and say " +
    "so in your output. Do NOT use it for things you can determine yourself.",
  inputSchema: {
    type: "object",
    properties: {
      question: {
        type: "string",
        description: "The single specific question. One fact, phrased so a busy " +
          "person can answer it in a sentence.",
      },
      context: {
        type: "string",
        description: "Why you are blocked and what you will do with the answer. " +
          "Lets the human spot a wrong question rather than answering it.",
      },
    },
    required: ["question"],
  },
};

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

function reply(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function fail(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

/** Text content, plus isError so the model can tell a fault from an answer. */
function content(text, isError) {
  return { content: [{ type: "text", text }], isError: !!isError };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * TICKET + POLL, not request/response.
 *
 * The obvious design — hold the HTTP request open until the human answers — does
 * not work here and the reason is structural, not a tuning problem: n8n's
 * Execute Workflow node does not await a child that suspends on a Wait node.
 * Measured, the webhook returned in 0.4s while nobody had been asked yet.
 *
 * So the ask and the answer are two separate events. n8n holds no state at all
 * here — it cannot write files (ADR 0006 addendum) and would not register a
 * second webhook — so the answer travels n8n -> router -> this runner's mailbox,
 * every hop a direction that already worked. We generate the ticket client-side
 * so there is nothing to parse out of a response we would otherwise have to
 * trust.
 */
async function askHuman(args) {
  const question = String((args && args.question) || "").trim();
  if (!question) {
    // Refuse at the seam rather than paging someone with a blank form.
    return content("ask_human called with an empty question — nothing was asked.", true);
  }
  if (!TOKEN) {
    return content(
      "ask_human is not configured: RUNNER_TOKEN is unset in this runner, so the " +
      "question endpoint would reject the call. No human was asked.", true);
  }

  const ticket = crypto.randomUUID();

  const body = JSON.stringify({
    ticket,
    question,
    context: String((args && args.context) || ""),
    source: "claude-runner",
    timeoutMinutes: Math.ceil(TIMEOUT_MS / 60000),
  });

  // 1. Raise the question. This returns immediately — it does NOT mean answered.
  let res;
  try {
    res = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "content-type": "application/json", "x-runner-token": TOKEN },
      body,
      signal: AbortSignal.timeout(30_000),
    });
  } catch (e) {
    // A network failure is NOT an answer. Say which happened — an agent told
    // "no answer" will reasonably proceed as if a human declined, and that is a
    // different fact from "the control plane was unreachable".
    return content(
      `ask_human could not reach the control plane (${String((e && e.message) || e)}). ` +
      `NO HUMAN WAS ASKED. Treat the fact as unknown and say so; do not assume ` +
      `the answer was "no".`, true);
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    return content(
      `ask_human could not raise the question: HTTP ${res.status}. NO HUMAN WAS ` +
      `ASKED. ${body.slice(0, 200)}`, true);
  }

  // 2. Poll the status endpoint. n8n cannot write files (ADR 0006 addendum), so
  // the answer is held in workflow static data and read back over HTTP — the one
  // transport already proven to work from this container.
  const deadline = Date.now() + TIMEOUT_MS;
  let consecutiveErrors = 0;
  while (Date.now() < deadline) {
    await sleep(POLL_MS);
    let data;
    try {
      const s = await fetch(`${STATUS_URL}?ticket=${encodeURIComponent(ticket)}`, {
        headers: { "x-runner-token": TOKEN },
        signal: AbortSignal.timeout(20_000),
      });
      if (!s.ok) throw new Error(`HTTP ${s.status}`);
      data = await s.json();
      consecutiveErrors = 0;
    } catch (e) {
      // A poll failure is TRANSIENT, not a verdict. Only give up if the endpoint
      // stays unreachable — otherwise a single blip would be reported to the
      // agent as "the human did not answer", which is a different fact.
      if (++consecutiveErrors >= 5) {
        return content(
          `ask_human lost contact with the control plane while waiting (${String((e && e.message) || e)}). ` +
          `The question WAS delivered; whether anyone answered is unknown. Do not ` +
          `treat this as a refusal.`, true);
      }
      continue;
    }

    if (data.state === "pending") continue;   // still with the human

    if (data.answered === true && data.answer) {
      return content(`A human answered:\n\n${data.answer}`);
    }
    // The distinction the whole gate exists to preserve.
    return content(
      `NO ANSWER. ${data.note || (data.timedOut ? "Nobody responded in time." : "The form returned nothing usable.")}\n` +
      `Treat this fact as UNKNOWN. Do not guess it, and do not read it as approval ` +
      `or refusal — state plainly in your output that you proceeded without it, or stop.`,
      true);
  }

  // "The question was delivered" is a claim this tool CANNOT make. A 200 from the
  // webhook means the control plane accepted the request, not that it reached a
  // person — the workflow can still fail downstream, and did during testing, so
  // the tool once reported delivery for a question nobody ever saw. Say what was
  // observed, not what was hoped.
  return content(
    `NO ANSWER within ${Math.round(TIMEOUT_MS / 60000)} minutes. The request was ` +
    `ACCEPTED by the control plane; whether it reached a person is not something ` +
    `this tool can confirm. So this is silence — NOT a refusal, and not proof ` +
    `anyone was asked. Treat the fact as UNKNOWN and say so in your output, or stop.`,
    true);
}

async function handle(msg) {
  const { id, method, params } = msg;
  if (method === "initialize") {
    return reply(id, {
      protocolVersion: (params && params.protocolVersion) || "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "ask-human", version: "1.0.0" },
    });
  }
  if (method === "notifications/initialized") return; // notification: no reply
  if (method === "tools/list") return reply(id, { tools: [TOOL] });
  if (method === "tools/call") {
    const name = params && params.name;
    if (name !== TOOL.name) return fail(id, -32602, `unknown tool: ${name}`);
    try {
      return reply(id, await askHuman(params.arguments || {}));
    } catch (e) {
      // Never let the tool crash the server: the agent would lose the session
      // rather than merely lose the answer.
      return reply(id, content(`ask_human failed: ${String((e && e.message) || e)}`, true));
    }
  }
  if (id !== undefined) fail(id, -32601, `method not found: ${method}`);
}

let buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      continue; // a malformed frame is not worth killing the session over
    }
    Promise.resolve(handle(msg)).catch(() => {});
  }
});
process.stdin.on("end", () => process.exit(0));
