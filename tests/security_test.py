#!/usr/bin/env python3
"""Security-posture tests over the deployment artifacts — the isolation and
fail-closed guarantees are the product, so they get regression protection that
runs WITHOUT Node or Docker (static analysis of the shipped config).

Covers:
  * Firewall scripts  — `bash -n` syntax, default-DROP policy, DNS pinned to
    docker's resolver (127.0.0.11), fail-closed entrypoints.
  * Auth fail-closed   — every server.js refuses to boot without its token.
  * docker-compose      — no runner publishes a host port; n8n bound to
    localhost; tokens use the required `${VAR:?}` form; the vault is mounted into
    the scrubber and into NO runner.
  * .gitignore          — secrets/PII/audit/control runtime state stay out of git.

    python tests/security_test.py     # exits non-zero on any failure
"""
import glob, json, os, sys, subprocess, yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)

def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()

def load_yaml(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return yaml.safe_load(f)

FIREWALLS = ["claude-runner/init-firewall.sh", "gemini-runner/init-firewall.sh"]
ENTRYPOINTS = ["claude-runner/entrypoint.sh", "gemini-runner/entrypoint.sh"]
SERVERS = ["router/server.js", "scrubber/server.js",
           "claude-runner/server.js", "gemini-runner/server.js"]

print("Firewall scripts — syntax + default-deny + DNS restriction:")
for fw in FIREWALLS:
    r = subprocess.run(["bash", "-n", os.path.join(ROOT, fw)], capture_output=True, text=True)
    check(f"{fw}: bash -n clean", r.returncode == 0, r.stderr.strip())
    txt = read(fw)
    for chain in ("INPUT", "FORWARD", "OUTPUT"):
        check(f"{fw}: default DROP on {chain}", f"iptables -P {chain} DROP" in txt)
    # DNS may only egress to docker's embedded resolver, never an arbitrary NS.
    check(f"{fw}: DNS pinned to 127.0.0.11", "--dport 53 -d 127.0.0.11" in txt)
    check(f"{fw}: no unrestricted DNS", "--dport 53 -j ACCEPT" not in txt)
    # Outbound HTTPS is allowlist-only (matched against the ipset), never blanket.
    check(f"{fw}: HTTPS egress is allowlist-only",
          "--dport 443 -m set --match-set allowed dst -j ACCEPT" in txt)

print("Entrypoints — fail CLOSED when the firewall can't apply:")
for ep in ENTRYPOINTS:
    r = subprocess.run(["bash", "-n", os.path.join(ROOT, ep)], capture_output=True, text=True)
    check(f"{ep}: bash -n clean", r.returncode == 0, r.stderr.strip())
    txt = read(ep)
    check(f"{ep}: aborts on firewall failure (Refusing to start)", "Refusing to start" in txt)
    check(f"{ep}: exits 1 on firewall failure", "exit 1" in txt)
    check(f"{ep}: strict bash flags", "set -euo pipefail" in txt)

print("Servers — auth fail CLOSED (refuse to boot without a token):")
for s in SERVERS:
    txt = read(s)
    check(f"{s}: refuses to start unset", "Refusing to start" in txt)
    check(f"{s}: process.exit(1) on missing token", "process.exit(1)" in txt)
    check(f"{s}: dev bypass gated on ALLOW_NO_AUTH", 'ALLOW_NO_AUTH' in txt)
    check(f"{s}: constant-time token compare", "timingSafeEqual" in txt)

print("docker-compose — no runner host port, n8n localhost, required tokens, vault split:")
compose = load_yaml("docker-compose.yml")
services = compose["services"]

def is_runner(name, cfg):
    if "runner" in name:
        return True
    b = cfg.get("build")
    bs = b if isinstance(b, str) else (b or {}).get("context", "")
    return "runner" in str(bs)

for name, cfg in services.items():
    if is_runner(name, cfg):
        check(f"{name}: publishes NO host port", not cfg.get("ports"), str(cfg.get("ports")))
        vols = cfg.get("volumes", []) or []
        mounts_vault = any(str(v).endswith(":/vault") or ":/vault:" in str(v) for v in vols)
        check(f"{name}: does NOT mount /vault", not mounts_vault)
        # a runner must never wholesale-mount the vault-adjacent token store
        check(f"{name}: no ./vault bind", not any(str(v).startswith("./vault") for v in vols))

# n8n reachable on LOOPBACK only — both stacks.
#
# This used to demand the literal "127.0.0.1:", which quietly forbade the IPv6
# loopback. That matters: on Windows `localhost` resolves to ::1 FIRST, so an
# IPv4-only publish makes every localhost:PORT request hang until it times out —
# which reads as "the service is broken", not "wrong address".
#
# "[::1]:" is loopback by definition and safe. "[::]:"  is the IPv6 equivalent of
# 0.0.0.0 and must stay forbidden, so this matches the loopback prefixes exactly
# rather than merely looking for a colon or a bracket.
LOOPBACK_BINDS = ("127.0.0.1:", "[::1]:")
n8n_ports = services["n8n"].get("ports", [])
check("n8n: all published ports bound to loopback (127.0.0.1 or [::1])",
      bool(n8n_ports) and all(str(p).startswith(LOOPBACK_BINDS) for p in n8n_ports),
      str(n8n_ports))
# Guard the guard: an all-interfaces IPv6 bind must NOT satisfy the check above.
check("the loopback check rejects [::] (all interfaces)",
      not "[::]:8080:8080".startswith(LOOPBACK_BINDS))

# scrubber DOES hold the vault (the one place token maps live).
scrub_vols = services["scrubber"].get("volumes", [])
check("scrubber: mounts /vault (token maps live here)",
      any(str(v).endswith(":/vault") for v in scrub_vols), str(scrub_vols))

# tokens must use the required ${VAR:?} form (fail-closed on unset env).
def env_of(cfg):
    return cfg.get("environment", []) or []
for name, cfg in services.items():
    for e in env_of(cfg):
        e = str(e)
        for tok in ("RUNNER_TOKEN=", "ROUTER_TOKEN=", "SCRUBBER_TOKEN=", "ASK_HUMAN_TOKEN="):
            if e.startswith(tok):
                check(f"{name}: {tok.rstrip('=')} uses required ${{VAR:?}} form", ":?" in e, e)

print("\nn8n does not hold the runners' credential (threat-model C3):")
# n8n used to carry RUNNER_TOKEN so the ask-human webhook could verify a runner.
# With env access in nodes on, that made any workflow authored in the n8n UI able
# to call a runner directly and skip the router. The door now has its own secret:
# the runner presents ASK_HUMAN_TOKEN and n8n verifies it. RUNNER_TOKEN never
# reaches n8n. tests/boundary_test.py holds the workflow-file half of this.
def env_names(cfg):
    return {str(e).split("=", 1)[0] for e in env_of(cfg)}
check("n8n: no RUNNER_TOKEN in its environment",
      "RUNNER_TOKEN" not in env_names(services.get("n8n", {})),
      sorted(env_names(services.get("n8n", {}))))
check("n8n: holds ASK_HUMAN_TOKEN to verify the ask-human door",
      "ASK_HUMAN_TOKEN" in env_names(services.get("n8n", {})))
for f in ("docker-compose.yml", "compose.scopes.yml"):
    svcs = (load_yaml(f).get("services") or {}) if os.path.exists(os.path.join(ROOT, f)) else {}
    for name, cfg in svcs.items():
        if cfg.get("build") in ("./claude-runner",) or str(cfg.get("build", "")).endswith("claude-runner"):
            check(f"{f} {name}: carries ASK_HUMAN_TOKEN (the ask-human MCP presents it)",
                  "ASK_HUMAN_TOKEN" in env_names(cfg))
_ask = read("claude-runner/ask-human-mcp.js")
check("ask-human-mcp presents x-ask-human-token to the n8n door",
      "x-ask-human-token" in _ask and "process.env.ASK_HUMAN_TOKEN" in _ask)
_mcp = json.loads(read("claude-runner/.mcp.json"))
check(".mcp.json passes ASK_HUMAN_TOKEN to the ask-human server",
      "ASK_HUMAN_TOKEN" in ((_mcp.get("mcpServers") or {}).get("ask-human", {}).get("env") or {}))

print("\nThe router is the ONLY entry point (threat-model C2):")
# C2 was closed on 2026-07-28 with NO regression test behind it, so one re-added
# HTTP node pointing at a runner would silently reopen it — and that is exactly
# how it arose the first time. Enforcement lives on the router: scope
# resolution, ethical walls, the PII guard, tier clamping and the audit record.
# A workflow that talks to a runner directly gets none of them.
#
# AND THEN THIS CHECK MISSED ONE, for the most ordinary reason available: the
# glob below covers `n8n-workflows/*.json`, and the violation — a shipped example
# workflow POSTing to http://claude-runner:8080/run — sat one directory up, at   # boundary-check: describes
# the repo root. The gate was green the whole time because the offending file was
# never in the set it scanned. Same family as the staged-only hygiene scan and the
# one-directional mount check: the logic was right and the INPUT was too small.
#
# scripts/boundary_check.py is now the tree-wide check and is what C2's
# asserted_by points at. This one stays because it is node-aware — it can name
# the offending node, which a text scan cannot.
_direct = []
for _p in glob.glob(os.path.join(ROOT, "n8n-workflows", "*.json")):
    try:
        _d = json.load(open(_p, encoding="utf-8"))
    except (OSError, ValueError):
        continue
    for _n in _d.get("nodes", []):
        _u = str((_n.get("parameters") or {}).get("url", ""))
        if "-runner:" in _u or "-runner." in _u:
            _direct.append(f"{os.path.basename(_p)}[{_n.get('name')}]")
check("no workflow calls a runner directly (C2)", not _direct, "; ".join(_direct))

print("\nResource ceilings — every service is bounded (threat-model M7):")
# M7 as written: "a verify fork-bomb takes the host." pids_limit is the control
# that actually closes it; mem/cpu bound the slower versions of the same problem.
#
# Asserted PER SERVICE rather than "at least one service has limits", because a
# single unbounded container is enough to take the host — and an aggregate check
# would pass with seven of eight covered, which is the shape of a gate that
# reassures without protecting.
for name, cfg in services.items():
    for key in ("mem_limit", "memswap_limit", "pids_limit"):
        check(f"{name}: declares {key}", cfg.get(key) is not None)
    # Without memswap_limit, a container at its memory ceiling escapes into host
    # swap and the limit only slows the failure down. Equal values forbid that.
    if cfg.get("mem_limit") and cfg.get("memswap_limit"):
        check(f"{name}: memswap_limit == mem_limit (no swap escape)",
              str(cfg["mem_limit"]) == str(cfg["memswap_limit"]),
              f'{cfg["mem_limit"]} vs {cfg["memswap_limit"]}')
    # A ceiling below observed idle use would OOM-kill the service on boot,
    # turning the guardrail into the outage.
    if cfg.get("pids_limit") is not None:
        check(f"{name}: pids_limit is a real bound, not a rubber stamp",
              0 < int(cfg["pids_limit"]) <= 4096, str(cfg.get("pids_limit")))

print("\ncompose.scopes.yml — the silo runners are equally locked down:")
scopes_compose = load_yaml("compose.scopes.yml")
for name, cfg in scopes_compose["services"].items():
    check(f"{name}: publishes NO host port", not cfg.get("ports"))
    vols = cfg.get("volumes", []) or []
    check(f"{name}: does NOT mount /vault",
          not any(":/vault" in str(v) for v in vols))
    # SCOPE_ROOT must be a PATH the runner actually has mounted, not a scope
    # NAME. The generator emitted the name ("delivery"), which resolves to
    # /workspace/delivery — a path that exists nowhere — so every request to
    # every silo runner would have been refused with "outside this runner's
    # scope root", reading as a security refusal rather than a config error.
    # An outage that looks like a working control is the worst possible shape.
    env_l = [str(e) for e in (cfg.get("environment") or [])]
    sr = next((e.split("=", 1)[1] for e in env_l if e.startswith("SCOPE_ROOT=")), None)
    if sr:
        targets = [str(v).split(":")[1] for v in (cfg.get("volumes") or [])
                   if len(str(v).split(":")) > 1 and str(v).split(":")[1].startswith("/workspace")]
        root_abs = "/workspace/" + sr.strip("/")
        check(f"{name}: SCOPE_ROOT is a mounted path, not a scope name",
              any(t == root_abs or t.startswith(root_abs + "/") or root_abs.startswith(t + "/")
                  or t == root_abs for t in targets),
              f"SCOPE_ROOT={sr!r} -> {root_abs}, but this runner mounts {targets}")

    # Bounded too, and asserted HERE rather than only over docker-compose.yml.
    # These are the silo runners that hold tenant work; the first pass bounded
    # the shared services, asserted it over the wrong file, and left all five of
    # these unbounded with the suite reporting green.
    for key in ("mem_limit", "memswap_limit", "pids_limit"):
        check(f"{name}: declares {key}", cfg.get(key) is not None)
    if cfg.get("mem_limit") and cfg.get("memswap_limit"):
        check(f"{name}: memswap_limit == mem_limit (no swap escape)",
              str(cfg["mem_limit"]) == str(cfg["memswap_limit"]))
    # Both halves of the in-runner PEP must be declared. SCOPE_ROOT was emitted
    # for months and read by nothing; a variable the compose file states and no
    # code acts on is a comment, so assert BOTH are present rather than assuming
    # the pair travels together.
    env = [str(e) for e in (cfg.get("environment") or [])]
    for var in ("SCOPE_ROOT=", "PII_POLICY="):
        check(f"{name}: declares {var.rstrip('=')}",
              any(e.startswith(var) for e in env), str(env))
    pol = next((e.split("=", 1)[1] for e in env if e.startswith("PII_POLICY=")), None)
    check(f"{name}: PII_POLICY is a known value",
          pol in ("block", "warn", "off"), repr(pol))

print("\nTool narrowing is ENFORCED, not merely allowed (threat-model H5):")
# --allowedTools adds permission ALLOW rules; the runners run with
# --dangerously-skip-permissions, which bypasses every permission check, so a
# list passed that way restricted nothing (probed 2026-09-17: Bash still ran).
# This is the static half; tests/unit/runner.test.mjs holds the behaviour.
def code_only(rel):
    return "\n".join(l for l in read(rel).splitlines() if not l.lstrip().startswith("//"))
_claude = code_only("claude-runner/server.js")
check("claude-runner: never passes --allowedTools (a no-op under skip-permissions)",
      '"--allowedTools"' not in _claude and '"--allowed-tools"' not in _claude)
check("claude-runner: narrows built-in tools with --tools", '"--tools"' in _claude)
check("claude-runner: narrows MCP servers with --disallowedTools", '"--disallowedTools"' in _claude)
_gemini = code_only("gemini-runner/server.js")
check("gemini-runner: reads allowedTools instead of ignoring it under --yolo",
      "geminiArgs(allowedTools)" in _gemini)
_risk = json.loads(read("router/policy.json"))["risk"]
_ta = _risk.get("toolAccess") or {}
check("policy: an unlisted tool is classified at the highest access (fail closed)",
      _risk["access"].get(_ta.get("unlisted"), 0) == max(v for k, v in _risk["access"].items() if k != "default"),
      repr(_ta.get("unlisted")))
check("policy: risk.enforce.actionBinding is present (a MISSING key enforces)",
      (_risk.get("enforce") or {}).get("actionBinding") in ("warn", "block"))

print(".gitignore — secrets / PII / runtime state excluded from source:")
gi = read(".gitignore")
for pat, why in [(".env", "secrets"), ("vault/", "PII token maps"),
                 ("audit/", "decision ledger"), ("control/*", "kill-switch state")]:
    check(f".gitignore covers {pat}  ({why})", pat in gi)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("All security-posture checks passed.")
