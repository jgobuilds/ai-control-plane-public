#!/usr/bin/env python3
"""the control plane installer — interactive bootstrap for the big decisions.

Asks the questions that shape a deployment (tokens, providers, scope hierarchy,
notification channel, optional ML detection), writes .env + context/scopes.json,
regenerates the derived artifacts, runs the conformance suite, and (optionally)
builds and starts the stack. Cross-platform, stdlib only.

    python scripts/install.py                 # interactive
    python scripts/install.py --dry-run       # show the plan, write nothing
    python scripts/install.py --non-interactive --up   # accept defaults, then build+up
    python scripts/install.py --help

Non-interactive mode (or a non-TTY) uses defaults + any value flags you pass.
Existing tokens in .env are reused; a placeholder/absent one is generated.
"""
import argparse, os, re, secrets, shutil, subprocess, sys, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")
ENV_EXAMPLE = os.path.join(ROOT, ".env.example")
SCOPES = os.path.join(ROOT, "context", "scopes.json")
TEMPLATES = os.path.join(ROOT, "templates", "scopes")
TOKEN_KEYS = ["RUNNER_TOKEN", "ROUTER_TOKEN", "SCRUBBER_TOKEN", "ASK_HUMAN_TOKEN"]

def c(s, code):  # tiny color, no-op if not a tty
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s
def hdr(s): print("\n" + c("== " + s + " ==", "1;36"))
def ok(s): print(c("  ✓ ", "32") + s)
def warn(s): print(c("  ! ", "33") + s)

def which(cmd): return shutil.which(cmd) is not None

def load_env(path):
    d = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
            if m: d[m.group(1)] = m.group(2)
    return d

def is_placeholder(v):
    return (not v) or v.startswith("change-me") or v.startswith("<")

def gen_token(): return secrets.token_hex(32)

def render_env(overrides):
    """Rewrite .env.example with overrides, preserving its comments/keys."""
    base = open(ENV_EXAMPLE, encoding="utf-8").read().splitlines() if os.path.exists(ENV_EXAMPLE) else []
    seen, out = set(), []
    for line in base:
        m = re.match(r"^([A-Z0-9_]+)=", line)
        if m and m.group(1) in overrides:
            out.append(f"{m.group(1)}={overrides[m.group(1)]}"); seen.add(m.group(1))
        else:
            out.append(line)
    for k, v in overrides.items():
        if k not in seen: out.append(f"{k}={v}")
    return "\n".join(out) + "\n"

def ask(interactive, prompt, default, choices=None):
    if not interactive:
        return default
    suffix = f" [{'/'.join(choices)}]" if choices else ""
    while True:
        r = input(c("? ", "36") + f"{prompt}{suffix} ({c(default,'1')}): ").strip()
        if not r: return default
        if choices and r not in choices:
            print(f"    choose one of: {', '.join(choices)}"); continue
        return r

def run(cmd, dry, cwd=ROOT):
    print(c("  $ ", "90") + " ".join(cmd))
    if dry: return 0
    return subprocess.call(cmd, cwd=cwd)

def main():
    ap = argparse.ArgumentParser(description="the control plane interactive installer.")
    ap.add_argument("--non-interactive", action="store_true", help="accept defaults + flags, no prompts")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; write/run nothing")
    ap.add_argument("--force", action="store_true", help="overwrite .env / scopes without backup prompt")
    ap.add_argument("--regen-tokens", action="store_true", help="regenerate all three tokens even if present")
    ap.add_argument("--dev", action="store_true", help="set ALLOW_NO_AUTH=1 (LOCAL DEV ONLY — unauthenticated)")
    ap.add_argument("--providers", choices=["claude", "claude+gemini"], default="claude+gemini")
    ap.add_argument("--notify-channel", choices=["slack", "teams", "webhook"], default="webhook")
    ap.add_argument("--slack-url", default=""); ap.add_argument("--teams-url", default=""); ap.add_argument("--webhook-url", default="")
    ap.add_argument("--scope-template", choices=["keep", "solo", "corp", "consulting"], default="keep")
    ap.add_argument("--scrub-backend", choices=["heuristic", "presidio", "dlp"], default="heuristic")
    ap.add_argument("--scrub-url", default="")
    ap.add_argument("--build", action="store_true", help="run docker compose build after config")
    ap.add_argument("--up", action="store_true", help="run docker compose up -d after config")
    args = ap.parse_args()

    interactive = sys.stdin.isatty() and not args.non_interactive
    print(c("\nthe control plane installer", "1;36"))
    print("Sets up config for the big decisions, then optionally builds and starts the stack.")

    hdr("Prerequisites")
    for tool, needed in [("docker", True), ("python", True), ("git", False)]:
        (ok if which(tool) else warn)(f"{tool} {'found' if which(tool) else 'NOT found'}")
    try:
        import yaml  # noqa
        ok("pyyaml found (needed by the generators)")
    except ImportError:
        warn("pyyaml missing — run `pip install pyyaml` (the artifact generators need it)")

    existing = load_env(ENV)

    hdr("Decisions")
    # Tokens — reuse existing unless placeholder/regen
    tokens = {}
    for k in TOKEN_KEYS:
        cur = existing.get(k, "")
        tokens[k] = gen_token() if (args.regen_tokens or is_placeholder(cur)) else cur
    reused = [k for k in TOKEN_KEYS if tokens[k] == existing.get(k) and not is_placeholder(existing.get(k, ""))]
    print(f"  Tokens: {'reusing '+', '.join(reused) if reused else f'generating {len(TOKEN_KEYS)} distinct secrets'}"
          + ("" if len(reused) in (0, len(TOKEN_KEYS)) else " (+generated the rest)"))

    providers = ask(interactive, "Which providers?", args.providers, ["claude", "claude+gemini"])
    channel = ask(interactive, "Notification channel", args.notify_channel, ["slack", "teams", "webhook"])
    url_key = {"slack": "SLACK_WEBHOOK_URL", "teams": "TEAMS_WEBHOOK_URL", "webhook": "NOTIFY_WEBHOOK_URL"}[channel]
    url_default = {"slack": args.slack_url, "teams": args.teams_url, "webhook": args.webhook_url}[channel] or existing.get(url_key, "")
    url = ask(interactive, f"{channel} webhook URL (blank = set later)", url_default)

    tmpl_avail = ["keep"] + ([d[:-5] for d in sorted(os.listdir(TEMPLATES))] if os.path.isdir(TEMPLATES) else [])
    scope_tmpl = ask(interactive, "Scope hierarchy template (keep = don't touch scopes.json)",
                     args.scope_template, [t for t in ["keep", "solo", "corp", "consulting"] if t in tmpl_avail or t == "keep"])

    scrub_backend = ask(interactive, "PII detection backend", args.scrub_backend, ["heuristic", "presidio", "dlp"])
    scrub_url = ask(interactive, "PII backend URL", args.scrub_url or existing.get("SCRUB_BACKEND_URL", "")) if scrub_backend != "heuristic" else ""

    dev = args.dev or (interactive and ask(interactive, "Enable ALLOW_NO_AUTH (local dev, UNAUTHENTICATED)?", "no", ["yes", "no"]) == "yes")

    # Build the .env overrides
    ov = dict(tokens)
    ov["NOTIFY_CHANNEL"] = channel
    if url: ov[url_key] = url
    if scrub_backend != "heuristic":
        ov["SCRUB_BACKEND"] = scrub_backend
        if scrub_url: ov["SCRUB_BACKEND_URL"] = scrub_url
    if dev: ov["ALLOW_NO_AUTH"] = "1"

    hdr("Plan")
    print(f"  .env               -> write {len(ov)} settings (channel={channel}, providers={providers}"
          + (f", scrub={scrub_backend}" if scrub_backend != 'heuristic' else "") + (", DEV/no-auth" if dev else "") + ")")
    print(f"  context/scopes.json-> {'leave as-is' if scope_tmpl=='keep' else 'apply template: '+scope_tmpl}")
    print("  generators         -> gen_compose, gen_usecase_register, gen_compliance_map")
    print("  conformance        -> scripts/conformance_test.py")
    docker_plan = " ".join([w for w, on in [("build", args.build), ("up -d", args.up)] if on]) or "skipped (run manually — see Next steps)"
    print(f"  docker             -> {docker_plan}")
    if dev: warn("ALLOW_NO_AUTH=1 makes every endpoint unauthenticated — LOCAL DEV ONLY.")

    if args.dry_run:
        print(c("\nDry run — nothing written.\n", "33")); return 0
    if interactive and ask(interactive, "Proceed?", "yes", ["yes", "no"]) != "yes":
        print("Aborted."); return 1

    hdr("Applying")
    # .env
    if os.path.exists(ENV) and not args.force:
        shutil.copy(ENV, ENV + ".bak"); ok(".env backed up to .env.bak")
    open(ENV, "w", encoding="utf-8").write(render_env(ov)); ok("wrote .env")
    # scopes template
    if scope_tmpl != "keep":
        src = os.path.join(TEMPLATES, scope_tmpl + ".json")
        if os.path.exists(src):
            if os.path.exists(SCOPES): shutil.copy(SCOPES, SCOPES + ".bak")
            shutil.copy(src, SCOPES); ok(f"applied scope template '{scope_tmpl}' (previous backed up)")
        else:
            warn(f"template {src} not found — kept existing scopes.json")
    # generators + conformance
    py = sys.executable
    for g in ["gen_compose.py", "gen_usecase_register.py", "gen_compliance_map.py"]:
        gp = os.path.join(ROOT, "scripts", g)
        if os.path.exists(gp): run([py, gp], False)
    hdr("Conformance")
    rc = run([py, os.path.join(ROOT, "scripts", "conformance_test.py")], False)
    (ok if rc == 0 else warn)("conformance " + ("passed" if rc == 0 else f"FAILED (exit {rc}) — review scopes.json"))

    # docker
    if args.build: run(["docker", "compose", "build"], False)
    if args.up: run(["docker", "compose", "up", "-d"], False)

    hdr("Next steps")
    print("  1. Log in the CLIs (one time):")
    print("       docker compose run --rm claude-runner claude login")
    if providers == "claude+gemini":
        print("       docker compose run --rm gemini-runner gemini     # then /auth")
    if not args.build: print("  2. docker compose build")
    if not args.up: print("  3. docker compose up -d")
    print("  4. Open n8n http://localhost:5678 (create the owner account), then follow QUICKSTART.md §2.6 smoke test.")
    if url_key not in ov: warn(f"No {channel} URL set — add {url_key} to .env before using notifications.")
    print(c("\nDone.\n", "1;32"))
    return 0

if __name__ == "__main__":
    sys.exit(main())
