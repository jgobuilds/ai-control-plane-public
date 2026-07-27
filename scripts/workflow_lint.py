#!/usr/bin/env python3
r"""Static checks on n8n workflow JSON — for the mistakes IMPORT ACCEPTS SILENTLY.

Every rule here was written after a real break. The common thread is that n8n
validates almost nothing at import: a malformed sub-workflow call imports "ok",
shows green in the UI, and fails only when the lane actually runs. Combined with
schedule-only lanes being un-runnable on demand, three separate defects survived
in "deployed" workflows until the first end-to-end execution.

  1. workflowId as a bare string. This n8n wants a resource locator; the plain
     form fails at run time with "No information about the workflow to execute
     found". Every workflow in the repo had this.
  2. A call to a sub-workflow that declares a typed input schema, with no
     workflowInputs mapping. The sub-workflow receives empty fields, so a
     notification posts with no title and no message — worse than failing,
     because it looks like it worked.
  3. Notification calls with waitForSubWorkflow:false. Fire-and-forget orphans
     the call and hides any error inside it. ~1s of waiting buys a visible failure.
  4. A scheduled lane with no callable trigger. `n8n execute` refuses a workflow
     whose only trigger is a schedule, so it cannot be tested until it fires.
  5. A lane with no errorWorkflow — a failure nobody hears about.

Exit 1 on any finding, so CI holds the line.

    python scripts/workflow_lint.py
"""
from __future__ import annotations
import json, os, sys, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, "n8n-workflows")

EXEC_WF = "n8n-nodes-base.executeWorkflow"
SCHEDULE = "n8n-nodes-base.scheduleTrigger"
CALLABLE = {"n8n-nodes-base.executeWorkflowTrigger", "n8n-nodes-base.webhook",
            "n8n-nodes-base.manualTrigger", "n8n-nodes-base.errorTrigger"}

# Sub-workflows whose trigger declares workflowInputs. A caller must map them.
NOTIFY_ID = "brightworks-notify"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def typed_input_subworkflows():
    """Discover which sub-workflows declare a typed input schema, rather than
    hardcoding a list that would rot the moment someone adds one."""
    typed = {}
    for p in glob.glob(os.path.join(WF_DIR, "*.json")):
        try:
            d = load(p)
        except ValueError:
            continue
        wid = d.get("id")
        for n in d.get("nodes", []):
            if n.get("type") == "n8n-nodes-base.executeWorkflowTrigger":
                vals = (n.get("parameters", {}).get("workflowInputs") or {}).get("values")
                if vals and wid:
                    typed[wid] = [v.get("name") for v in vals]
    return typed


def check(path, typed):
    name = os.path.basename(path)
    findings = []
    try:
        d = load(path)
    except ValueError as e:
        return [(name, f"is not valid JSON: {e}")]

    nodes = d.get("nodes", [])
    is_example = name.endswith(".example.json")

    for n in nodes:
        if n.get("type") != EXEC_WF:
            continue
        par = n.get("parameters", {})
        wid_raw = par.get("workflowId")
        wid = wid_raw.get("value") if isinstance(wid_raw, dict) else wid_raw

        if wid and not isinstance(wid_raw, dict):
            findings.append((name, f"node {n['name']!r}: workflowId is a bare string; "
                                   "n8n needs a resource locator and fails at RUN time"))
        if wid in typed and not par.get("workflowInputs"):
            findings.append((name, f"node {n['name']!r}: calls {wid!r}, which declares "
                                   f"inputs {typed[wid]}, without a workflowInputs "
                                   "mapping — the sub-workflow gets empty fields"))
        if wid == NOTIFY_ID and par.get("options", {}).get("waitForSubWorkflow") is not True:
            findings.append((name, f"node {n['name']!r}: notification is fire-and-forget; "
                                   "a failure inside Notify would be invisible"))

    has_schedule = any(n.get("type") == SCHEDULE for n in nodes)
    if has_schedule and not is_example:
        if not any(n.get("type") in CALLABLE for n in nodes):
            findings.append((name, "schedule-only: `n8n execute` cannot run it, so it "
                                   "cannot be tested before its schedule fires"))
        if not d.get("settings", {}).get("errorWorkflow"):
            findings.append((name, "scheduled lane has no errorWorkflow — a failure "
                                   "would be silent"))
    return findings


def main():
    files = sorted(glob.glob(os.path.join(WF_DIR, "*.json")))
    if not files:
        # Say it plainly rather than exiting 0 on an empty sweep.
        print(f"NOTHING CHECKED — no workflow JSON found in {WF_DIR}")
        return 1
    typed = typed_input_subworkflows()
    all_findings = []
    for p in files:
        all_findings += check(p, typed)

    print(f"Workflow lint — {len(files)} file(s), "
          f"{len(typed)} typed-input sub-workflow(s): {', '.join(typed) or 'none'}")
    if not all_findings:
        print("  clean.")
        return 0
    for fname, msg in all_findings:
        print(f"  {fname}: {msg}")
    print(f"\n  {len(all_findings)} finding(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
