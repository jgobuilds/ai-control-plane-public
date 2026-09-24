// The recurrence-review lane's Code node, tested outside n8n.
//
// WHY THIS EXISTS. `n8n execute` cannot run a workflow while the server holds
// port 5679, so the Code node is the one hop a deploy-time end-to-end call
// cannot exercise — and it is the hop that decides whether a human is told
// anything. Every other node in that lane is either a schedule trigger, an HTTP
// call proven by calling it, or the same Notify sub-workflow ten other lanes use.
//
// It loads the jsCode string out of the COMMITTED workflow JSON and runs that,
// so editing the workflow and forgetting the test is not possible: there is only
// one copy of the code.
//
// The fixture mirrors a real payload captured from the running lane. The cases
// are the ones where a monitoring lane goes quietly wrong: it must not read as
// healthy when the call failed, and it must not read as quiet when a source is
// blind — no CI failures and nobody collecting produce the identical empty list.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WF = path.join(HERE, "..", "..", "n8n-workflows", "recurrence-review.workflow.json");
const wf = JSON.parse(readFileSync(WF, "utf8"));
const code = wf.nodes.find(n => n.name === "Format recurrence review").parameters.jsCode;

function run(payload) {
  const $input = { first: () => ({ json: payload }) };
  return new Function("$input", code)($input)[0].json;
}

const LIVE = {
  generated: "2026-08-07T09:20:00+00:00",
  windowDays: 90, threshold: 2, haveData: true,
  registerEntries: 4, registerErrors: [],
  sources: {
    router: "recomputed live from the ledger (13 fault(s))",
    ci: "last seen 2026-08-07 (0d ago)",
    lane: "nothing recorded — has `recurrence.py collect` run?",
  },
  rows: [
    { signature: "router:runner error", count: 7, first: "2026-08-01T10:06:50.335Z",
      last: "2026-08-01T11:20:29.999Z", sources: ["router"], status: "harness-fixed",
      title: "A runner returned 5xx" },
    { signature: "ci:cause:vendored-file-fails-the-linter", count: 2,
      first: "2026-08-03T15:45:00Z", last: "2026-08-03T21:32:00Z", sources: ["ci"],
      status: "watching", title: "A known-cause row false-positives" },
  ],
  harnessDebt: [], dispositionsDue: [],
};

const COLLECTED = {
  ...LIVE,
  sources: { router: "recomputed live from the ledger (0 fault(s))",
             ci: "last seen 2026-08-05 (2d ago)", lane: "last seen 2026-08-05 (2d ago)" },
};

test("a blind source is a WARN, not a quiet week", () => {
  const r = run(LIVE);
  assert.equal(r.severity, "warn");
  assert.match(r.title, /^Recurrence: nothing recurring, but 1 source\(s\) blind/);
  assert.ok(r.message.includes("recurrence.py collect"),
            "the body has to say what to do about it");
});

test("a STALE source is a WARN — a dead collector is not a quiet fortnight", () => {
  // The host-side collect is a Scheduled Task and can die. When it does, the
  // note goes from "last seen 2026-08-05 (2d ago)" to the same sentence with a
  // bigger number — true, unalarming, and wrong. The word STALE is what the
  // formatter escalates on, so it is tested here and not only in the collector.
  const r = run({ ...COLLECTED, rows: [], sources: {
    router: "recomputed live from the ledger (0 fault(s))",
    ci: "STALE — last seen 2026-06-01 (67d ago); has collect stopped?",
    lane: "last seen 2026-08-05 (2d ago)" } });
  assert.equal(r.severity, "warn");
  assert.match(r.title, /1 source\(s\) blind/);
  assert.ok(r.message.includes("has collect stopped?"));
});

test("harness debt outranks every other alarm and names the signature", () => {
  const r = run({ ...LIVE, harnessDebt: [
    { signature: "ci:boom", count: 4, first: "2026-08-01T00:00:00Z",
      last: "2026-08-06T00:00:00Z", sources: ["ci"] }] });
  assert.equal(r.severity, "warn");
  assert.equal(r.title, "1 pattern(s) failed twice with no harness fix — ci:boom");
  assert.ok(r.message.includes("reliability/recurrence.yaml"));
});

test("an expired disposition is its own alarm, and carries the next action", () => {
  const r = run({ ...COLLECTED, dispositionsDue: [
    { match: "ci:x", title: "A thing", status: "watching", until: "2026-08-01",
      expired: true, next_action: "fix it upstream in ai-standards" }] });
  assert.equal(r.severity, "warn");
  assert.equal(r.title, "1 recurrence disposition(s) EXPIRED — re-decide them");
  assert.ok(r.message.includes("fix it upstream in ai-standards"));
});

test("due-soon is a prompt, not an ambush — info, said before it goes red", () => {
  const r = run({ ...COLLECTED, dispositionsDue: [
    { match: "ci:x", title: "A thing", status: "accepted", until: "2026-08-15",
      expired: false, next_action: "" }] });
  assert.equal(r.severity, "info");
  assert.equal(r.title, "1 recurrence disposition(s) come due within 14 days");
});

test("a quiet, fully-collected week is the only info-and-silent case", () => {
  const r = run({ ...COLLECTED, rows: [] });
  assert.equal(r.severity, "info");
  assert.equal(r.title, "Recurrence: nothing new failed twice");
  // Coverage before totals: an empty count must say it counted nothing.
  assert.ok(r.message.includes("(nothing at all"));
});

test("a register that could not be read is reported, never treated as empty", () => {
  const r = run({ ...COLLECTED, registerEntries: 0,
                  registerErrors: ["pyyaml is not installed"] });
  assert.equal(r.severity, "warn");
  assert.equal(r.title, "The recurrence register could not be read");
  assert.ok(r.message.includes("pyyaml is not installed"));
});

test("a failed lane call never reads as a clean bill of health", () => {
  const r = run({ error: "connect ECONNREFUSED lanes:8081" });
  assert.equal(r.severity, "warn");
  assert.ok(r.title.includes("lane call itself failed"));
});

test("the payload the lane emits is the shape Notify consumes", () => {
  const r = run(LIVE);
  for (const k of ["severity", "title", "message", "source"]) {
    assert.ok(typeof r[k] === "string" && r[k].length, `${k} must be a non-empty string`);
  }
  assert.equal(r.source, "recurrence-review");
});
