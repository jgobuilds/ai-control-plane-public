#!/usr/bin/env python3
r"""n8n bootstrap — import workflows into a running n8n via the CLI, no UI clicks.

Turns the manual "Workflows → Import from File" step into a reproducible command,
so deploys are code, not clicks. Import upserts by the JSON's top-level `id`, so
stable ids (e.g. "aicp-notify") make cross-workflow references (a Notify
sub-workflow called by many) deterministic without querying assigned ids.

Topology: a running container (default `n8n`). On Windows the Docker engine lives
in WSL, so docker is invoked through `wsl -e bash -lc`; on POSIX, directly. Each
workflow is `docker cp`'d in, then imported by the in-container `n8n import:workflow`.

  python scripts/n8n_bootstrap.py --import                       # all non-example *.json
  python scripts/n8n_bootstrap.py --import notify.subworkflow.json daily-digest.workflow.json
  python scripts/n8n_bootstrap.py --dry-run aicp-daily-digest   # execute once, no activate
  python scripts/n8n_bootstrap.py --activate aicp-daily-digest
  python scripts/n8n_bootstrap.py --list

Pure stdlib. Windows + WSL-Docker friendly.
"""
import os, sys, argparse, subprocess, shlex

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, "n8n-workflows")
IS_WIN = os.name == "nt"


def wsl_path(win_path):
    r"""C:\path\to\x -> /mnt/c/path/to/x  (so the WSL docker daemon can read it)."""
    p = os.path.abspath(win_path)
    drive, rest = os.path.splitdrive(p)
    if drive:
        return "/mnt/" + drive[0].lower() + rest.replace("\\", "/")
    return p.replace("\\", "/")


_MODE = None


def docker_mode():
    """Which Docker daemon to drive: "direct" (docker on PATH) or "wsl".

    This used to assume WSL unconditionally on Windows. After moving the stack to
    Docker Desktop (ADR 0009) that assumption pointed every command at the OLD,
    stopped engine — imports failed with "container ... is not running" while a
    perfectly healthy container sat in the other daemon. Two daemons on one
    machine is a real topology, so DETECT rather than assume.

    Override with N8N_BOOTSTRAP_DOCKER=direct|wsl.
    """
    global _MODE
    if _MODE:
        return _MODE
    forced = os.environ.get("N8N_BOOTSTRAP_DOCKER", "").strip().lower()
    if forced in ("direct", "wsl"):
        _MODE = forced
        return _MODE
    if not IS_WIN:
        _MODE = "direct"
        return _MODE
    try:
        probe = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30)
        _MODE = "direct" if probe.returncode == 0 and probe.stdout.strip() else "wsl"
    except (OSError, subprocess.SubprocessError):
        _MODE = "wsl"
    return _MODE


def sh(args):
    """Run a docker command as an ARGV LIST on whichever daemon is reachable.

    argv, not a shell string: the previous version interpolated single-quoted
    paths, which bash accepts and Windows cmd does not — so going "direct" on
    Windows would have mangled every path with a space.

    encoding/errors are pinned: without them Python decodes with the Windows ANSI
    codepage (cp1252) and a single non-ANSI byte in docker/n8n output raises
    UnicodeDecodeError, leaving stdout None and crashing the caller.
    """
    if docker_mode() == "wsl":
        # WSL still needs one shell string; quote each argument for bash.
        argv = ["wsl", "-e", "bash", "-lc", " ".join(shlex.quote(a) for a in args)]
    else:
        argv = list(args)
    return subprocess.run(argv, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def host_path(path):
    """The path form the TARGET daemon understands — /mnt/d/... only for WSL."""
    return wsl_path(path) if docker_mode() == "wsl" else os.path.abspath(path)


def cp_and_import(container, path):
    base = os.path.basename(path)
    src = host_path(path)
    dst = f"/tmp/n8n-import-{base}"
    r1 = sh(["docker", "cp", src, f"{container}:{dst}"])
    if r1.returncode != 0:
        return False, (r1.stderr or r1.stdout).strip()
    r2 = sh(["docker", "exec", container, "n8n", "import:workflow", f"--input={dst}"])
    blob = (r2.stdout + r2.stderr).strip()
    ok = r2.returncode == 0 and "Successfully imported" in blob
    return ok, (blob.splitlines()[-1] if blob else "")


def list_workflows(container, active=None):
    flag = [f"--active={active}"] if active else []
    return [l for l in sh(["docker", "exec", container, "n8n", "list:workflow"] + flag).stdout.splitlines()
            if "|" in l]


# Activating from the CLI sets active=1 in the database but does NOT register a
# Schedule Trigger with the running process — the workflow reads as active and
# never fires. Same class as the webhook-registration lesson in ADR 0006, found
# again here by checking the EXECUTION count instead of trusting the flag.
# "Activated" and "running" are different facts.
RESTART_HINT = (
    "\n  NOTE: a CLI activation does not register schedules or webhooks with the"
    "\n  running n8n. Restart it, or the workflow reads as active and never fires:"
    "\n    docker compose restart n8n"
)


def set_active(container, wid, state):
    """Activate/deactivate, preferring the current verb and falling back.

    n8n 2.30 deprecated `update:workflow --active=` in favour of
    `publish:workflow` / `unpublish:workflow`. It still works and prints a
    deprecation notice ON STDOUT ALONGSIDE THE SUCCESS — which is worth naming,
    because a caller grepping the combined output for "error" or "deprecated"
    reads a working call as a failed one. That is not hypothetical: a check here
    reported ERROR for `aicp-error-trigger` purely because the workflow's own
    NAME contains "rror".

    Try the current verb first; fall back only when the command is absent, not
    when it merely fails. Falling back on any failure would silently retry a
    genuine error against a deprecated path and report whatever that returned.
    """
    verb = "publish:workflow" if state == "true" else "unpublish:workflow"
    r = sh(["docker", "exec", container, "n8n", verb, f"--id={wid}"])
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        return True, out
    # Older n8n: the command does not exist. oclif says so explicitly; any other
    # failure is a real one and must not be retried against the legacy verb.
    if "not found" in out.lower() or "command " in out.lower() and "does not exist" in out.lower():
        r = sh(["docker", "exec", container, "n8n", "update:workflow",
                f"--id={wid}", f"--active={state}"])
        return r.returncode == 0, (r.stdout + r.stderr).strip() + "  [via deprecated update:workflow]"
    return False, out


def dry_run(container, wid):
    r = sh(["docker", "exec", container, "n8n", "execute", f"--id={wid}"])
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def main():
    ap = argparse.ArgumentParser(description="Bootstrap n8n workflows via the CLI (no UI).")
    ap.add_argument("--container", default="n8n")
    ap.add_argument("--import", dest="do_import", nargs="*", metavar="FILE",
                    help="import these files (default: all non-example *.json in n8n-workflows/)")
    ap.add_argument("--dry-run", metavar="ID", help="execute a workflow once (no activate)")
    ap.add_argument("--activate", metavar="ID", help="set active=true")
    ap.add_argument("--deactivate", metavar="ID", help="set active=false")
    ap.add_argument("--list", action="store_true", help="list imported id|name")
    args = ap.parse_args()
    c = args.container

    if args.do_import is not None:
        files = args.do_import or sorted(
            f for f in os.listdir(WF_DIR) if f.endswith(".json") and ".example." not in f)

        # `n8n import:workflow` defaults to --activeState false, i.e. it DEACTIVATES
        # everything it imports. Re-importing a running lane therefore switches it
        # off silently — which surfaces days later as "why did the schedule stop?".
        # Capture what was live and restore it, so an import never changes run state.
        active_before = {l.split("|")[0].strip()
                         for l in list_workflows(c, active="true") if "|" in l}

        print(f"Importing {len(files)} workflow(s) into '{c}':")
        fail = 0
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(WF_DIR, os.path.basename(f))
            ok, msg = cp_and_import(c, path)
            print(f"  {'ok  ' if ok else 'FAIL'}  {os.path.basename(f)}" + ("" if ok else f"  — {msg}"))
            fail += 0 if ok else 1
        if fail:
            print(f"\n{fail} import(s) failed.", file=sys.stderr)
            return 1

        still_active = {l.split("|")[0].strip()
                        for l in list_workflows(c, active="true") if "|" in l}
        dropped = sorted(active_before - still_active)
        for wid in dropped:
            ok, msg = set_active(c, wid, "true")
            print(f"  {'restored active' if ok else 'FAILED to restore'}: {wid}"
                  + ("" if ok else f" — {msg}"))

    if args.dry_run:
        ok, msg = dry_run(c, args.dry_run)
        print(f"Dry-run {args.dry_run}: {'✓' if ok else '✗'}")
        print("  " + "\n  ".join(msg.splitlines()[-8:]))
        if not ok:
            return 1

    if args.activate:
        ok, msg = set_active(c, args.activate, "true")
        print(f"activate {args.activate}: {'✓' if ok else '✗ ' + msg}")
        if not ok:
            return 1
        print(RESTART_HINT)

    if args.deactivate:
        ok, msg = set_active(c, args.deactivate, "false")
        print(f"deactivate {args.deactivate}: {'✓' if ok else '✗ ' + msg}")

    if args.list:
        print("Workflows (id|name):")
        for l in list_workflows(c):
            print("  " + l)
    return 0


if __name__ == "__main__":
    sys.exit(main())
