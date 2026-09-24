#!/usr/bin/env python3
"""Prove the runner's tool restrictions still bite on a given Claude Code build.

WHY THIS EXISTS. claude-runner restricts what an agent can use with two CLI flags:
`--tools <built-ins>` and `--disallowedTools mcp__<server>`. Those are vendor
behaviours, not our code — and they have already surprised us once: the runner
originally passed `--allowedTools`, which a live probe on 2026-09-17 showed
restricts NOTHING under --dangerously-skip-permissions. The same day an unpinned
rebuild moved production from 2.1.220 to 2.1.274 while the flags had only ever been
probed on 2.1.220. Every CLI upgrade can change this silently, and no unit test can
see it, because the behaviour lives in the vendor binary.

So before a Claude Code version is promoted, run this against it. Four sessions, in
pairs, each with a positive control:

    bash control       no restriction                -> Bash exposed, and runs
    bash restricted    --tools Read                  -> Bash not exposed, and does not run
    mcp control        --mcp-config (runner's)       -> mcp__ tools exposed, servers connected
    mcp restricted     + --disallowedTools <servers> -> no mcp__ tools exposed

WHAT DECIDES THE VERDICT — the CLI's own tool list, not the model's words. With
`--output-format stream-json --verbose` the CLI emits a `system/init` event listing
exactly the tools the session exposes and each MCP server's connection status. The
first version of this script asked the model to list its mcp__ tools; on one run it
answered in prose that MENTIONED "mcp__" while describing the config file, and the
probe reported enforcement BROKEN when it was fine. A verdict that depends on how a
model phrases an answer is not a verdict. The Bash pair additionally asks the model
to run a command, as a behavioural confirmation that the exposed list is what is
enforced — its answer (the literal command output, or a refusal) has no phrasing
ambiguity.

THE CONTROLS ARE THE POINT. A restricted session exposing no Bash proves nothing if
the unrestricted one exposed none either — an auth failure or an outage produce the
same empty list. A restriction only counts when its control was positive in the same
run. Outcomes:
    0  enforcement proven     every control positive, every restriction held
    1  enforcement BROKEN     a control positive and its restriction leaked
    2  inconclusive           a control negative, a server not connected, or output
                              unreadable — NOT a pass

    python scripts/probe_cli_enforcement.py --image <runner-image> [--token-from <runner>]
    python scripts/probe_cli_enforcement.py --container claude-runner-commons

--image runs throwaway containers from a built image; the OAuth token comes from
CLAUDE_CODE_OAUTH_TOKEN or a running runner (--token-from) and reaches docker by
NAME only, so it never appears on a command line or in output. --container probes a
live runner in place, with its own credentials, as its runtime user. Cost: four
short calls on the model below.

Stdlib only.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys

MODEL = "claude-haiku-4-5-20251001"
TIMEOUT_S = 180
MCP_CONFIG = "/app/.mcp.json"
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
MARK = "PROBE_RAN_OK"

BASH_PROMPT = (f"Run this exact shell command with the Bash tool: echo {MARK} . Reply with only the "
               "command output, or the single word NOBASH if you have no Bash tool.")
MCP_PROMPT = "Reply with the single word OK."


def redact(s):
    return SECRET.sub("[redacted]", str(s))


# ---------------------------------------------------------------- decision (pure)

def parse_stream(stdout):
    """stream-json lines -> {tools, servers, result, error} or None if there is no init event."""
    init, final = None, None
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init" and init is None:
            init = ev
        elif ev.get("type") == "result":
            final = ev
    if init is None:
        return None
    return {
        "tools": [t for t in (init.get("tools") or []) if isinstance(t, str)],
        "servers": {s.get("name"): s.get("status") for s in (init.get("mcp_servers") or [])
                    if isinstance(s, dict)},
        "result": str((final or {}).get("result") or ""),
        "error": bool((final or {}).get("is_error")) if final else True,
    }


def evaluate(probes):
    """probes: {bash_control, bash_restricted, mcp_control, mcp_restricted} -> parsed or None.
    Returns (exit_code, lines). Pure — the tested half."""
    lines, broken, inconclusive = [], [], []

    def mcp_tools(p):
        return [t for t in p["tools"] if t.startswith("mcp__")]

    for name in ("bash_control", "bash_restricted", "mcp_control", "mcp_restricted"):
        p = probes.get(name)
        if p is None:
            lines.append(f"  {name:<16} NO INIT EVENT (CLI did not start a session)")
            continue
        lines.append(f"  {name:<16} exposes {len(p['tools'])} tools; Bash={'Bash' in p['tools']}; "
                     f"mcp__={len(mcp_tools(p))}; servers={p['servers']}; "
                     f"result={redact(p['result'])[:60]!r}")

    # Built-in tools: declared (init list) AND behavioural (did the command run).
    bc, br = probes.get("bash_control"), probes.get("bash_restricted")
    if bc is None or "Bash" not in bc["tools"] or MARK not in bc["result"]:
        inconclusive.append("bash control did not expose AND run Bash — the restricted result proves nothing")
    elif br is None:
        inconclusive.append("bash restricted session produced no init event")
    elif "Bash" in br["tools"] or MARK in br["result"]:
        broken.append("--tools Read did NOT remove Bash "
                      f"(exposed={'Bash' in br['tools']}, ran={MARK in br['result']})")

    # MCP servers: declared only — the init tool list is what the session can call.
    mc, mr = probes.get("mcp_control"), probes.get("mcp_restricted")
    if mc is None or not mcp_tools(mc):
        inconclusive.append("mcp control exposed no mcp__ tools — the restricted result proves nothing")
    elif any(st != "connected" for st in mc["servers"].values()) or not mc["servers"]:
        inconclusive.append(f"mcp control servers not all connected ({mc['servers']}) — "
                            "an absent tool could be a connection failure, not enforcement")
    elif mr is None:
        inconclusive.append("mcp restricted session produced no init event")
    elif mcp_tools(mr):
        broken.append(f"--disallowedTools did NOT remove the MCP servers ({mcp_tools(mr)[:3]} exposed)")

    if broken:
        return 1, lines + [""] + [f"BROKEN: {b}" for b in broken]
    if inconclusive:
        return 2, lines + [""] + [f"INCONCLUSIVE: {i}" for i in inconclusive]
    return 0, lines + ["", "ENFORCEMENT PROVEN: both restrictions held, each against a positive control."]


# ---------------------------------------------------------------- execution

def make_runner(args, env):
    def run(argv, pass_prompt=True):
        if args.container:
            cmd = ["docker", "exec", "-u", args.user, "-e", "HOME=/tmp/probe-home", "-w", "/tmp"]
            if pass_prompt:
                cmd += ["-e", "PROBE_PROMPT=" + env["PROBE_PROMPT"]]
            cmd += [args.container]
        else:
            cmd = ["docker", "run", "--rm", "--user", args.user, "-e", "HOME=/tmp/probe-home",
                   "-w", "/tmp", "--entrypoint", "", "-e", "CLAUDE_CODE_OAUTH_TOKEN"]
            if pass_prompt:
                cmd += ["-e", "PROBE_PROMPT"]
            cmd += [args.image]
        cmd += ["sh", "-c", 'mkdir -p "$HOME" && exec "$@"', "sh", *argv]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=TIMEOUT_S + 60)
    return run


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--image", help="a built runner image to probe in throwaway containers")
    target.add_argument("--container", help="a running runner container to probe in place")
    ap.add_argument("--token-from", help="(--image only) read CLAUDE_CODE_OAUTH_TOKEN from this "
                                         "running container instead of the environment")
    ap.add_argument("--user", default="node", help="runtime user to probe as (default: node)")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args(argv)

    env = dict(os.environ)
    if args.image:
        if args.token_from:
            r = subprocess.run(["docker", "exec", args.token_from, "printenv", "CLAUDE_CODE_OAUTH_TOKEN"],
                               capture_output=True, text=True)
            env["CLAUDE_CODE_OAUTH_TOKEN"] = r.stdout.strip()
        if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            print("INCONCLUSIVE: no CLAUDE_CODE_OAUTH_TOKEN (set it, or pass --token-from <runner>)")
            return 2
    run = make_runner(args, env)

    ver = run(["claude", "--version"], pass_prompt=False)
    print(f"Probing {'image ' + args.image if args.image else 'container ' + args.container} "
          f"as {args.user} — claude {redact(ver.stdout.strip() or ver.stderr.strip())[:60]}")
    cfg = run(["cat", MCP_CONFIG], pass_prompt=False)
    try:
        servers = sorted(json.loads(cfg.stdout).get("mcpServers", {}))
    except (ValueError, AttributeError):
        servers = []
    if not servers:
        print(f"INCONCLUSIVE: no MCP servers readable from {MCP_CONFIG} in the target")
        return 2
    print(f"MCP servers from the runner's config: {', '.join(servers)}")

    common = ["--output-format", "stream-json", "--verbose", "--model", args.model,
              "--dangerously-skip-permissions"]
    plan = {
        "bash_control":    (BASH_PROMPT, ["--max-turns", "3"]),
        "bash_restricted": (BASH_PROMPT, ["--max-turns", "3", "--tools", "Read"]),
        "mcp_control":     (MCP_PROMPT,  ["--max-turns", "1", "--mcp-config", MCP_CONFIG]),
        "mcp_restricted":  (MCP_PROMPT,  ["--max-turns", "1", "--mcp-config", MCP_CONFIG,
                                          "--disallowedTools", ",".join(f"mcp__{s}" for s in servers)]),
    }
    probes = {}
    for name, (prompt, extra) in plan.items():
        env["PROBE_PROMPT"] = prompt
        # The prompt reaches the CLI through the environment and is expanded by the
        # shell inside the container, so host-side quoting cannot mangle it.
        argv = ["sh", "-c", 'exec claude -p "$PROBE_PROMPT" "$@"', "sh", *common, *extra]
        try:
            probes[name] = parse_stream(run(argv).stdout)
        except subprocess.TimeoutExpired:
            probes[name] = None
    code, lines = evaluate(probes)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
