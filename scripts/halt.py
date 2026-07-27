#!/usr/bin/env python3
"""Kill switch CLI — flip control/halt.json to halt or resume agent activity.

The router (router/killswitch.js) reads control/halt.json at the TOP of every
decision and refuses to dispatch when the global switch is on, or when the
request's scope (or ANY ancestor) is halted. This CLI is how a human operator
throws that switch — instantly, without a redeploy.

    python scripts/halt.py global "prod incident 1234"     # halt EVERYTHING
    python scripts/halt.py scope client-a "data question"  # halt a scope + its descendants
    python scripts/halt.py resume global                   # clear the global halt
    python scripts/halt.py resume client-a                 # un-halt one scope
    python scripts/halt.py resume                           # clear ALL halts
    python scripts/halt.py status                           # show current state

Honors CONTROL_DIR (default: <repo>/control). Writes control/halt.json — which is
gitignored runtime state; only control/halt.example.json is committed.
"""
import os, sys, json, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROL_DIR = os.environ.get("CONTROL_DIR") or os.path.join(ROOT, "control")
HALT_FILE = os.path.join(CONTROL_DIR, "halt.json")

EMPTY = {"global": False, "scopes": [], "reason": "", "ts": ""}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def load():
    try:
        with open(HALT_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(EMPTY)
    return {
        "global": bool(s.get("global", False)),
        "scopes": [x for x in s.get("scopes", []) if isinstance(x, str) and x],
        "reason": s.get("reason") if isinstance(s.get("reason"), str) else "",
        "ts": s.get("ts") if isinstance(s.get("ts"), str) else "",
    }


def save(state):
    os.makedirs(CONTROL_DIR, exist_ok=True)
    with open(HALT_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def status(state):
    if state["global"]:
        print(f'HALTED (global) — reason: {state["reason"] or "(none)"} @ {state["ts"] or "?"}')
    elif state["scopes"]:
        print(f'HALTED (scopes: {", ".join(state["scopes"])}) — reason: {state["reason"] or "(none)"} @ {state["ts"] or "?"}')
    else:
        print("not halted")


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, state = argv[0], load()

    if cmd == "status":
        status(state)
        return 0

    if cmd == "global":
        state["global"] = True
        state["reason"] = argv[1] if len(argv) > 1 else ""
        state["ts"] = now()
        save(state)
        print("HALTED globally.")
        status(state)
        return 0

    if cmd == "scope":
        if len(argv) < 2:
            print("usage: halt.py scope <name> [reason]")
            return 2
        name = argv[1]
        if name not in state["scopes"]:
            state["scopes"].append(name)
        state["reason"] = argv[2] if len(argv) > 2 else ""
        state["ts"] = now()
        save(state)
        print(f'HALTED scope "{name}".')
        status(state)
        return 0

    if cmd == "resume":
        target = argv[1] if len(argv) > 1 else None
        if target is None:
            save(dict(EMPTY))
            print("Resumed ALL (global + every scope).")
            return 0
        if target == "global":
            state["global"] = False
        elif target in state["scopes"]:
            state["scopes"].remove(target)
        else:
            print(f'scope "{target}" was not halted.')
            status(state)
            return 0
        # Clear reason/ts once nothing is halted anymore.
        if not state["global"] and not state["scopes"]:
            state["reason"], state["ts"] = "", ""
        save(state)
        print(f"Resumed {target}.")
        status(state)
        return 0

    print(f"unknown command: {cmd}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
