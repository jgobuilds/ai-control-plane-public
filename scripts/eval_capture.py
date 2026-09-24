#!/usr/bin/env python3
"""Capture a production failure into a regression dataset case (the close-the-loop
step of the agent improvement loop; see IMPROVEMENT-LOOP.md).

Governed observability: if a scrubber URL+token is supplied, the prompt is POSTed to
the scrubber /scrub endpoint FIRST, so the stored case holds TOKENIZED text
(«EMAIL_1», never raw PII). Datasets live on the deploy volume, per-scope, local —
they are .gitignored (except eval/datasets/starter/).

Two ways in:
  1. Direct — one case from flags:
       python scripts/eval_capture.py --dataset acme --scope acme \\
           --prompt @/path/to/prompt.txt --task-type plan \\
           --grader-type judge --goal "Plan must not skip the rollback step" \\
           --source prod-failure \\
           --scrubber-url http://scrubber:8080 --scrubber-token $SCRUBBER_TOKEN
  2. Ingest — promote flagged cases the n8n feedback lane dropped into eval/inbox/:
       python scripts/eval_capture.py --dataset acme --ingest eval/inbox

Pure stdlib (urllib). Windows-friendly.
"""
import sys, os, json, re, time, hashlib, argparse, urllib.request, urllib.error

ALLOWED_GRADERS = ("exact", "contains", "regex", "not_contains", "not_regex", "judge")


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def slug(text, n=6):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def make_id(task_type, prompt):
    return f"{(task_type or 'case')}-{time.strftime('%Y%m%d')}-{slug((prompt or '') + str(time.time()))}"


def read_prompt(value):
    """`@path` reads a file; anything else is the literal prompt text."""
    if value and value.startswith("@"):
        with open(value[1:], encoding="utf-8") as f:
            return f.read()
    return value or ""


def scrub_prompt(text, scope, scrubber_url, token):
    """POST to the scrubber /scrub so the STORED prompt is tokenized, never raw PII.
    Returns (tokenized_text, findings). Fails LOUD — we would rather refuse to write
    a case than silently persist raw tenant PII into a dataset."""
    payload = json.dumps({"text": text, "scope": scope}).encode("utf-8")
    req = urllib.request.Request(
        scrubber_url.rstrip("/") + "/scrub", data=payload, method="POST",
        headers={"content-type": "application/json", "x-scrubber-token": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    return out.get("text", text), out.get("findings", {})


def build_grader(gtype, value, goal):
    if gtype not in ALLOWED_GRADERS:
        raise ValueError(f"grader-type {gtype!r} not in {ALLOWED_GRADERS}")
    if gtype == "judge":
        if not goal:
            raise ValueError("judge grader needs --goal")
        return {"type": "judge", "goal": goal}
    if value in (None, ""):
        raise ValueError(f"{gtype} grader needs --grader-value")
    return {"type": gtype, "value": value}


def write_case(dataset, case):
    out_dir = os.path.join(repo_root(), "eval", "datasets", dataset)
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, case["id"] + ".json")
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        json.dump(case, f, indent=2)
        f.write("\n")
    return dest


def capture_direct(args):
    prompt = read_prompt(args.prompt)
    if not prompt:
        raise ValueError("--prompt (text or @file) is required for a direct capture")
    findings = {}
    if args.scrubber_url:
        if not args.scrubber_token:
            raise ValueError("--scrubber-url given but --scrubber-token missing")
        prompt, findings = scrub_prompt(prompt, args.scope, args.scrubber_url, args.scrubber_token)
    grader = build_grader(args.grader_type, args.grader_value, args.goal)
    case = {
        "id": args.id or make_id(args.task_type, prompt),
        "scope": args.scope,
        "task_type": args.task_type,
        "action": args.action,
        "prompt": prompt,
        "grader": grader,
        "source": args.source,
        "created": time.strftime("%Y-%m-%d"),
    }
    dest = write_case(args.dataset, case)
    print(f"wrote {dest}"
          + (f"  (tokenized PII: {json.dumps(findings)})" if findings else ""))
    return 0


def capture_ingest(args):
    """Promote flagged cases the feedback workflow dropped into eval/inbox/.

    Each inbox file is a thumbs-down record the human left:
        { scope, prompt, output?, verdict?, note?, task_type?, action?, grader? }
    We turn it into a regression case (tokenizing the prompt if a scrubber is set).
    A thumbs-down means the output was WRONG, so the default grader is a judge whose
    goal is the human's note — the case should PASS once the fix lands."""
    inbox = args.ingest
    if not os.path.isdir(inbox):
        raise ValueError(f"--ingest dir not found: {inbox}")
    promoted, moved_dir = [], os.path.join(inbox, "promoted")
    for name in sorted(os.listdir(inbox)):
        if not name.endswith(".json"):
            continue
        fp = os.path.join(inbox, name)
        with open(fp, encoding="utf-8") as f:
            rec = json.load(f)
        scope = rec.get("scope") or args.scope
        if not scope:
            raise ValueError(f"{name}: no scope (set one in the record or pass --scope)")
        prompt = rec.get("prompt", "")
        findings = {}
        if args.scrubber_url and prompt:
            if not args.scrubber_token:
                raise ValueError("--scrubber-url given but --scrubber-token missing")
            prompt, findings = scrub_prompt(prompt, scope, args.scrubber_url, args.scrubber_token)
        grader = rec.get("grader")
        if not grader:
            goal = rec.get("note") or "The output correctly and safely satisfies the prompt."
            grader = {"type": "judge", "goal": goal}
        task_type = rec.get("task_type", "generate")
        case = {
            "id": rec.get("id") or make_id(task_type, prompt),
            "scope": scope,
            "task_type": task_type,
            "action": rec.get("action", "advise"),
            "prompt": prompt,
            "grader": grader,
            "source": rec.get("source", "feedback-flag"),
            "created": rec.get("created", time.strftime("%Y-%m-%d")),
        }
        dest = write_case(args.dataset, case)
        promoted.append(dest)
        # Archive the consumed inbox record so it isn't promoted twice.
        os.makedirs(moved_dir, exist_ok=True)
        os.replace(fp, os.path.join(moved_dir, name))
        print(f"promoted {name} -> {dest}"
              + (f"  (tokenized PII: {json.dumps(findings)})" if findings else ""))
    if not promoted:
        print("no *.json cases to promote in the inbox.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Capture a prod failure into a regression dataset case.")
    ap.add_argument("--dataset", required=True, help="target dataset name (eval/datasets/<name>)")
    ap.add_argument("--scope", default=None, help="scope the case belongs to (governance boundary)")
    ap.add_argument("--prompt", default=None, help="prompt text, or @path to read from a file")
    ap.add_argument("--task-type", default="generate", dest="task_type")
    ap.add_argument("--action", default="advise", help="advise|read|write|ingest|apply|send|execute")
    ap.add_argument("--grader-type", default="judge", dest="grader_type", choices=ALLOWED_GRADERS)
    ap.add_argument("--grader-value", default=None, dest="grader_value",
                    help="expected value/pattern for exact|contains|regex graders")
    ap.add_argument("--goal", default=None, help="goal text for a judge grader")
    ap.add_argument("--source", default="prod-failure")
    ap.add_argument("--id", default=None, help="explicit case id (else derived)")
    ap.add_argument("--scrubber-url", default=os.environ.get("SCRUBBER_URL"), dest="scrubber_url",
                    help="if set, tokenize the prompt via POST <url>/scrub before storing")
    ap.add_argument("--scrubber-token", default=os.environ.get("SCRUBBER_TOKEN"), dest="scrubber_token")
    ap.add_argument("--ingest", default=None,
                    help="promote flagged cases dropped by the feedback lane from this dir")
    args = ap.parse_args(argv)

    try:
        if args.ingest:
            return capture_ingest(args)
        if not args.scope:
            raise ValueError("--scope is required for a direct capture")
        return capture_direct(args)
    except (ValueError, OSError) as e:
        print(f"eval_capture: {e}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print(f"eval_capture: scrubber unreachable: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
