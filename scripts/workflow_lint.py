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
import json, os, re, sys, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, "n8n-workflows")

EXEC_WF = "n8n-nodes-base.executeWorkflow"
SCHEDULE = "n8n-nodes-base.scheduleTrigger"
CALLABLE = {"n8n-nodes-base.executeWorkflowTrigger", "n8n-nodes-base.webhook",
            "n8n-nodes-base.manualTrigger", "n8n-nodes-base.errorTrigger"}

# Sub-workflows whose trigger declares workflowInputs. A caller must map them.
NOTIFY_ID = "aicp-notify"

# LIVE EXCEPTION, recorded rather than silenced — the .trivyignore convention:
# "an entry here is a decision, not a silencing; it stays only while its
# justification holds."
#
# Every aicp-notify caller uses autoMapInputData. That is genuinely fragile — it
# matches on field NAME and yields blanks when one is renamed — but it is not
# broken today: each caller's Code node emits {severity,title,message,source}
# verbatim, and notifications demonstrably arrive. Converting eight callers to
# defineBelow means re-importing eight live workflows, which is its own change
# window with its own verification, and bundling it into the approval-gate work
# would put two unrelated risks in one commit.
#
# Scoped to notify ONLY. The approval gate gets no exception: it is the control
# a human acts on, and a blank field there is a rubber stamp rather than a
# missing log line.
#
# REMOVE WHEN: the notify callers are converted, or any of them renames a field.
AUTOMAP_EXCEPTION = {NOTIFY_ID}

# Exceptions are PRINTED, never silent. An allowed-but-fragile thing you cannot
# see is the same as one you never checked.
WARNINGS = []

CODE_NODE = "n8n-nodes-base.code"
HTTP_NODE = "n8n-nodes-base.httpRequest"
ROUTER_CALL = re.compile(r"router:8080/(route|fanout)\b")
DECLARES_ACTION = re.compile(r"""["']?\baction["']?\s*:""")
DECLARES_TRIGGER = re.compile(r"""["']?\btrigger["']?\s*:""")
WEBHOOK = "n8n-nodes-base.webhook"
PASSTHROUGH_BODY = re.compile(r"^=\{\{\s*JSON\.stringify\(\$json\.payload\)\s*\}\}$")


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
        if wid in typed:
            wi = par.get("workflowInputs") or {}
            # A PRESENT mapping object is not a mapping. All three callers shipped
            # {"mappingMode": "autoMapInputData", "value": {}} — truthy, so the
            # original `not par.get(...)` check passed on every one of them while
            # nothing was mapped. That is the same vacuous-pass shape this file
            # exists to catch, occurring in this file.
            if not wi:
                findings.append((name, f"node {n['name']!r}: calls {wid!r}, which declares "
                                       f"inputs {typed[wid]}, without a workflowInputs "
                                       "mapping — the sub-workflow gets empty fields"))
            else:
                mode = wi.get("mappingMode")
                mapped = {k for k, v in (wi.get("value") or {}).items()
                          if str(v).strip() != ""}
                if wid in AUTOMAP_EXCEPTION and mode == "autoMapInputData":
                    WARNINGS.append((name, f"node {n['name']!r}: auto-maps {wid!r} — "
                                           "allowed by AUTOMAP_EXCEPTION, fragile by name. "
                                           "See the constant for the removal trigger"))
                    continue
                if mode != "defineBelow":
                    findings.append((name, f"node {n['name']!r}: calls {wid!r} with "
                                           f"mappingMode={mode!r}. Auto-mapping a TYPED "
                                           "sub-workflow matches on field NAME and yields "
                                           "blanks when they differ — silently. Use "
                                           "'defineBelow' and state what you pass"))
                missing = [f for f in typed[wid] if f not in mapped]
                if missing:
                    findings.append((name, f"node {n['name']!r}: calls {wid!r} without "
                                           f"mapping {missing} — declared inputs that would "
                                           "arrive blank"))
        if wid == NOTIFY_ID and par.get("options", {}).get("waitForSubWorkflow") is not True:
            findings.append((name, f"node {n['name']!r}: notification is fire-and-forget; "
                                   "a failure inside Notify would be invisible"))

    # What starts this lane, if nobody is at the keyboard when it runs.
    types = {n.get("type") for n in nodes}
    unattended = "schedule" if SCHEDULE in types else ("webhook" if WEBHOOK in types else None)

    for n in nodes:
        par = n.get("parameters", {})
        # n8n's image ships no python (hardened, no package manager — ADR 0006),
        # so its internal Python runner is marked unavailable at boot and a
        # Python Code node fails when it RUNS, not when it is imported. The boot
        # log's "Python 3 is missing" warning is that, and it is expected.
        # Python belongs in the `lanes` service, driven over HTTP.
        if n.get("type") == CODE_NODE and str(par.get("language", "")).startswith("python"):
            findings.append((name, f"node {n['name']!r}: Python Code node — n8n here has no "
                                   "Python runner, so it fails at run time. Put the Python in "
                                   "the `lanes` service and call it over HTTP (ADR 0006)"))

        # A lane that routes work must say what the work DOES. The router scores
        # blast radius from `action`, and an omitted verb is scored as "advise"
        # (threat-model H5): under actionBinding:"warn" that silently skips
        # requireVerify, and under "block" it is a 400. A passthrough body carries
        # its caller's declaration, so it is reported, not failed.
        url = str(par.get("url", ""))
        if n.get("type") == HTTP_NODE and ROUTER_CALL.search(url):
            body = str(par.get("jsonBody", ""))
            if PASSTHROUGH_BODY.search(body):
                WARNINGS.append((name, f"node {n['name']!r}: forwards its caller's body to "
                                       f"{url}; `action` is whatever that caller declared"))
            elif not DECLARES_ACTION.search(body):
                findings.append((name, f"node {n['name']!r}: calls {url} without declaring "
                                       "`action` — the router scores it as 'advise' "
                                       "(threat-model H5). Declare the verb the prompt needs"))
            # An UNATTENDED lane must also say it is unattended. An omitted
            # trigger scores as a human-initiated turn (autonomy 1), which skips
            # requireVerify: research-watch sent a misspelt "schedule" and
            # incident-responder sent nothing, and both were scored as if a
            # person had asked. Schedule-started -> "scheduled"; webhook-started
            # -> "event" (policy.json risk.autonomy).
            if (not PASSTHROUGH_BODY.search(body) and unattended
                    and not DECLARES_TRIGGER.search(body)):
                findings.append((name, f"node {n['name']!r}: this lane starts on a "
                                       f"{unattended} but calls {url} without declaring "
                                       "`trigger` — the router scores it as a human turn. "
                                       "Declare 'scheduled' or 'event'"))

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
    if WARNINGS:
        print(f"  {len(WARNINGS)} allowed exception(s) — see AUTOMAP_EXCEPTION "
              f"for the removal trigger:")
        for fname, msg in WARNINGS:
            print(f"    WARN  {fname}: {msg}")
    if not all_findings:
        print("  clean.")
        return 0
    for fname, msg in all_findings:
        print(f"  {fname}: {msg}")
    print(f"\n  {len(all_findings)} finding(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
