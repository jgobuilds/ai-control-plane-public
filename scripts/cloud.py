#!/usr/bin/env python3
"""Resolve a capability to a cloud provider — and refuse loudly when it is not built.

The engine ships vendor-neutral the same way it ships brand-neutral: an
organization picks its provider in `context/cloud-providers.json`, and nothing
above this file knows which one it got.

WHY REFUSE RATHER THAN FALL BACK. A capability that quietly degrades to nothing
is how a control plane comes to believe it tokenized something it did not, or
transcribed a call it never touched. Every unimplemented path here raises with
the reason, the provider that IS implemented, and what building it would need.
`unsupported` and `working` must never look the same — the same rule the
diagnostician's no-log tier exists for.

TODAY: **google is the only provider with any implementation, and even that is
`stub`** — the shape exists, the calls do not. Azure and AWS are declared so the
seam is real and the egress is costed, not so anyone believes they work.

THE SEAM IS NOT PROVEN BY ITS SHAPE. `ticket.py` learned this: a seam with one
implementation is an assumption, not a seam. This one is honest about being an
assumption until a second provider is actually built — which is why `--audit`
prints the count rather than a green tick.

    python scripts/cloud.py --list
    python scripts/cloud.py --capability transcription
    python scripts/cloud.py --capability transcription --provider aws   # refuses
    python scripts/cloud.py --egress            # every domain a choice implies
    python scripts/cloud.py --audit             # what is actually built

Pure stdlib.
"""
from __future__ import annotations
import argparse, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "context", "cloud-providers.json")


class NotBuilt(RuntimeError):
    """Raised for a declared-but-unimplemented provider. Never caught internally —
    the caller must see it, because the alternative is silent degradation."""


def load(path=None):
    with open(path or REGISTRY, encoding="utf-8") as fh:
        return json.load(fh)


def resolve(capability, provider=None, reg=None):
    """The provider record for a capability. Raises NotBuilt unless implemented.

    `stub` resolves — it is a shape a caller may inspect and plan against — but
    callers must check `status` before assuming a call will work. `--audit`
    exists so nobody has to guess which is which.
    """
    reg = reg or load()
    caps = reg.get("capabilities", {})
    if capability not in caps:
        raise NotBuilt(
            f"no such capability {capability!r}. Known: "
            f"{', '.join(sorted(caps)) or '(none)'}")
    cap = caps[capability]
    name = provider or reg.get("default_provider")
    if name not in cap["providers"]:
        raise NotBuilt(
            f"{capability}: no provider {name!r}. Declared: "
            f"{', '.join(sorted(cap['providers']))}")
    rec = dict(cap["providers"][name], provider=name, capability=capability,
               why=cap.get("why", ""))
    if rec.get("status") == "unsupported":
        built = [p for p, r in cap["providers"].items()
                 if r.get("status") in ("implemented", "stub")]
        raise NotBuilt(
            f"{capability} via {name!r} is DECLARED BUT NOT BUILT.\n"
            f"  service : {rec.get('service', '?')}\n"
            f"  notes   : {rec.get('notes', '')}\n"
            f"  built   : {', '.join(built) or 'nothing yet'}\n"
            f"  Refusing rather than falling back — a capability that silently "
            f"does nothing is worse than one that stops.")
    return rec


def egress(reg=None):
    """{provider: sorted domains} across every capability.

    Selecting a provider is an EXPLICIT egress decision. Runners are default-deny,
    so this is the list that would have to be opened — and seeing it before
    choosing is the point.
    """
    reg = reg or load()
    out = {}
    for cap in reg.get("capabilities", {}).values():
        for name, rec in cap["providers"].items():
            out.setdefault(name, set()).update(rec.get("egress", []))
    return {k: sorted(v) for k, v in sorted(out.items())}


def audit(reg=None):
    """Counts by status. Deliberately not a pass/fail — there is nothing to pass."""
    reg = reg or load()
    rows, tally = [], {}
    for cname, cap in sorted(reg.get("capabilities", {}).items()):
        for pname, rec in sorted(cap["providers"].items()):
            st = rec.get("status", "?")
            tally[st] = tally.get(st, 0) + 1
            rows.append((cname, pname, st, rec.get("service", "")))
    return rows, tally


def main(argv=None):
    ap = argparse.ArgumentParser(description="Resolve a capability to a cloud provider.")
    ap.add_argument("--capability")
    ap.add_argument("--provider")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--egress", action="store_true")
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args(argv)
    reg = load()

    if a.list:
        print(f"default provider: {reg.get('default_provider')!r}")
        for cname, cap in sorted(reg.get("capabilities", {}).items()):
            built = [f"{p}({r.get('status')})" for p, r in sorted(cap["providers"].items())
                     if r.get("status") != "unsupported"]
            print(f"  {cname:16s} {', '.join(built) or 'NOTHING BUILT'}")
        return 0

    if a.egress:
        for p, doms in egress(reg).items():
            print(f"  {p}:")
            for d in doms:
                print(f"    {d}")
        print("\n  Runners are default-deny. Choosing a provider means opening "
              "these\n  deliberately — the list is here so that is a decision, "
              "not a discovery.")
        return 0

    if a.audit:
        rows, tally = audit(reg)
        for c, p, st, svc in rows:
            mark = {"implemented": "✓", "stub": "~", "unsupported": "✗"}.get(st, "?")
            print(f"  {mark} {c:16s} {p:8s} {st:12s} {svc[:52]}")
        print(f"\n  {tally.get('implemented', 0)} implemented · "
              f"{tally.get('stub', 0)} stub · {tally.get('unsupported', 0)} not built")
        # A seam with one provider is an assumption, not a seam. Say so until a
        # second one exists, rather than letting the shape imply portability.
        real = {p for _, p, st, _ in rows if st in ("implemented", "stub")}
        if len(real) < 2:
            print(f"  NOTE: only {', '.join(sorted(real)) or 'nothing'} has any "
                  f"implementation. The seam is UNPROVEN until a second provider "
                  f"is built —\n  a shape with one implementation is an "
                  f"assumption wearing an interface.")
        return 0

    if not a.capability:
        ap.error("give --capability, or one of --list / --egress / --audit")
    try:
        rec = resolve(a.capability, a.provider, reg)
    except NotBuilt as e:
        print(f"{e}", file=sys.stderr)
        return 2
    print(json.dumps(rec, indent=2, ensure_ascii=False) if a.as_json else
          "\n".join(f"  {k:10s} {v}" for k, v in rec.items()
                    if k not in ("notes",)) + f"\n  notes      {rec.get('notes', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
