#!/usr/bin/env python3
"""Prove the status page cannot lie in the two ways it is most likely to.

WHY THIS EXISTS. A status page is a control like any other, so it gets the same
treatment: assert the failure, not the happy path.

1. **A prompt must never become a chart label.** The router writes
   `error:<message>` for faults, and runner stderr once carried the full
   `claude -p <prompt>` line into the ledger (finding D3). Bucketing on the raw
   message would have painted it on screen. `block_category()` maps to a fixed
   label set; this pins that, including the D3-shaped record.

2. **A section that cannot read its source must not raise.** The page's whole job
   is to report trouble. If a missing ledger takes the renderer down, the one
   moment it matters is the one moment it is blank.

    python tests/status_data_test.py
"""
import io, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

fails = []


def check(label, got, want):
    if got != want:
        fails.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
    print(("  ok   " if got == want else "  FAIL ") + label)


# The ledger path is read at import, so point it somewhere synthetic first.
tmp = tempfile.mkdtemp(prefix="status-test-")
os.environ["AICP_AUDIT_LOG"] = os.path.join(tmp, "decisions.jsonl")
import status_data as sd                                       # noqa: E402

print("block_category — the labels are fixed, the messages are not")
CASES = [
    ("halted", "halted"),                                   # policy values pass through
    ("approval-required", "approval-required"),
    ("error:scope acme is retired", "retired scope"),
    ("error:unknown scope foo", "unknown scope"),
    ("error:getaddrinfo ENOTFOUND gemini-runner", "runner unreachable"),
    ("error:blockCrossVendor: data 3", "cross-vendor blocked"),
    ("error:PII detected in prompt", "PII refused"),
    ("error:runner returned 500", "runner error"),
    ("error:no runner-map entry for jane", "scope not mapped"),
    ("", "unknown"),
    (None, "unknown"),
]
for raw, want in CASES:
    check(f"{raw!r} -> {want}", sd.block_category(raw), want)

# The actual D3 record shape: a whole prompt trailing the message. Whatever the
# bucket is called, it must not be the prompt.
D3 = ("error:runner returned 500: /bin/sh -c claude -p 'summarise the Q3 "
      "contract for ACME and flag any indemnity clause' --output-format json")
cat = sd.block_category(D3)
check("a prompt-shaped message is truncated to a bucket", len(cat) <= 48, True)
check("...and the prompt text is not in the label",
      "indemnity" in cat or "ACME" in cat, False)

print("\n_counted — biggest first, so the chart order is not insertion order")
check("ordering", list(sd._counted(["a", "b", "b", "c", "b", "c"])), ["b", "c", "a"])

print("\nledger — degrades, never raises")
check("missing file reports an error instead of throwing",
      "error" in sd.ledger(), True)
check("...and still answers the count", sd.ledger().get("records"), 0)

with io.open(os.environ["AICP_AUDIT_LOG"], "w", encoding="utf-8", newline="\n") as f:
    f.write("deadbeef {\"ts\":\"2026-08-06T00:00:00Z\",\"scope\":\"acme\",\"tier\":\"cheap\"}\n")
    f.write("this line is garbage and must be skipped, not fatal\n")
    f.write("cafebabe {\"ts\":\"2026-08-06T00:01:00Z\",\"scope\":\"acme\",\"blocked\":\"halted\"}\n")
    f.write("f00d {not json at all}\n")
led = sd.ledger()
check("parses the good records", led["records"], 2)
check("counts the block", led["blocked"], 1)
check("categorises it", led["blockedReasons"], {"halted": 1})

print("\nlanes / sessions — a broken source is reported, not raised")
# Set the module global, not the env var: it is resolved at import, and the
# first version of this test set the env afterwards, ran against the REAL image,
# and passed by measuring nothing.
sd.SESSION_IMAGE = "ai-control-plane-does-not-exist:0"
check("an unreadable session volume returns an error field",
      "error" in sd.sessions(), True)

try:
    os.remove(os.environ["AICP_AUDIT_LOG"])
    os.rmdir(tmp)
except OSError:
    pass

if fails:
    print("\nFAIL:\n  - " + "\n  - ".join(fails))
    sys.exit(1)
print("\nPASS status_data")
