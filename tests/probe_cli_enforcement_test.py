#!/usr/bin/env python3
"""Prove the enforcement probe's verdict can't pass — or fail — for the wrong reason.

probe_cli_enforcement.py drives the real vendor CLI, so it cannot run in CI (it needs
credentials and spends model calls). Its DECISION can, and the decision is where a
probe like this goes wrong silently. evaluate() is pure; every way it could be fooled
is synthesised here from the CLI's stream-json `system/init` events.

    controls positive, restrictions held                  -> 0 proven
    Bash exposed under --tools Read                       -> 1 broken
    Bash not listed but the command still ran             -> 1 broken
    mcp__ tools exposed under --disallowedTools           -> 1 broken
    REGRESSION: restricted answer mentions "mcp__" in
      prose while exposing none                           -> 0 proven (not broken)
    control exposes nothing (auth failure)                -> 2 inconclusive, NOT proven
    control lists Bash but the command did not run        -> 2
    an MCP server not connected in the control            -> 2
    no init event                                         -> 2
    token-shaped strings are redacted in the report

    python tests/probe_cli_enforcement_test.py
"""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import probe_cli_enforcement as pce  # noqa: E402

fails = []
ALL = ["Bash", "Read", "Edit", "Write", "mcp__ask-human__ask_human", "mcp__n8n__get_node"]
CONNECTED = {"n8n": "connected", "ask-human": "connected"}


def expect(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def p(tools, result="", servers=None, error=False):
    return {"tools": tools, "servers": servers if servers is not None else {}, "result": result, "error": error}


GOOD = {
    "bash_control": p(ALL, pce.MARK, CONNECTED),
    "bash_restricted": p(["Read"], "NOBASH"),
    "mcp_control": p(ALL, "OK", CONNECTED),
    "mcp_restricted": p(["Bash", "Read", "Edit", "Write"], "OK", CONNECTED),
}


def with_(**over):
    d = dict(GOOD)
    d.update(over)
    return d


def main():
    print("The enforcement verdict:")
    code, lines = pce.evaluate(GOOD)
    expect("controls positive + restrictions held -> 0", code == 0, "\n".join(lines))

    code, _ = pce.evaluate(with_(bash_restricted=p(["Read", "Bash"], "NOBASH")))
    expect("Bash exposed under --tools Read -> 1", code == 1)
    code, _ = pce.evaluate(with_(bash_restricted=p(["Read"], pce.MARK)))
    expect("Bash absent from the list but the command ran -> 1", code == 1)
    code, _ = pce.evaluate(with_(mcp_restricted=p(["Read", "mcp__n8n__get_node"], "OK", CONNECTED)))
    expect("mcp__ tools exposed under --disallowedTools -> 1", code == 1)

    print("\nThe verdict comes from the CLI's tool list, not the model's words:")
    prose = ("I've read the file. The .mcp.json configures n8n and ask-human, but I have no "
             "mcp__ tools available.")
    code, lines = pce.evaluate(with_(mcp_restricted=p(["Read"], prose, CONNECTED)))
    expect("REGRESSION: prose mentioning mcp__ with none exposed -> 0, not broken", code == 0, "\n".join(lines))

    print("\nA restriction only counts against a positive control:")
    code, lines = pce.evaluate(with_(bash_control=p([], "Invalid API key", error=True)))
    expect("control exposes nothing (auth failure) + restricted empty -> 2", code == 2, "\n".join(lines))
    code, _ = pce.evaluate(with_(bash_control=p(ALL, "I will not run commands")))
    expect("control lists Bash but did not run it -> 2", code == 2)
    code, _ = pce.evaluate(with_(mcp_control=p(ALL, "OK", {"n8n": "connected", "ask-human": "failed"})))
    expect("an MCP server not connected in the control -> 2", code == 2)
    code, _ = pce.evaluate(with_(mcp_control=p(["Bash", "Read"], "OK", CONNECTED)))
    expect("control exposes no mcp__ tools -> 2", code == 2)
    code, _ = pce.evaluate(with_(bash_restricted=None))
    expect("no init event -> 2", code == 2)

    print("\nParsing and redaction:")
    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "tools": ["Read"],
                    "mcp_servers": [{"name": "n8n", "status": "connected"}]}),
        json.dumps({"type": "assistant", "message": {}}),
        json.dumps({"type": "result", "result": "OK", "is_error": False}),
    ])
    got = pce.parse_stream(stream)
    expect("stream-json parses init + result",
           got == {"tools": ["Read"], "servers": {"n8n": "connected"}, "result": "OK", "error": False}, got)
    expect("output with no init event parses to None",
           pce.parse_stream("Error: claude native binary not installed") is None)
    _, lines = pce.evaluate(with_(bash_control=p(ALL, pce.MARK + " sk-ant-oat01-abcDEF_123-xyz", CONNECTED)))
    expect("token-shaped strings are redacted in the report",
           "sk-ant-" not in "\n".join(lines) and "[redacted]" in "\n".join(lines), "\n".join(lines))

    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
        return 1
    print("All enforcement-probe checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
