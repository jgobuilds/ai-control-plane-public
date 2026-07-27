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
import os, sys, subprocess, yaml

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
        for tok in ("RUNNER_TOKEN=", "ROUTER_TOKEN=", "SCRUBBER_TOKEN="):
            if e.startswith(tok):
                check(f"{name}: {tok.rstrip('=')} uses required ${{VAR:?}} form", ":?" in e, e)

print("compose.scopes.yml — the silo runners are equally locked down:")
scopes_compose = load_yaml("compose.scopes.yml")
for name, cfg in scopes_compose["services"].items():
    check(f"{name}: publishes NO host port", not cfg.get("ports"))
    vols = cfg.get("volumes", []) or []
    check(f"{name}: does NOT mount /vault",
          not any(":/vault" in str(v) for v in vols))

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
