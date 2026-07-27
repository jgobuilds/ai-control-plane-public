#!/usr/bin/env python3
"""PII scrub / rehydrate tests — runnable WITHOUT Node (CI-now).

scrubber/server.js is the SOURCE OF TRUTH for the tokenization. Because node is
not in this environment, this file MIRRORS the same PATTERNS (regexes) and the
deterministic token-vault logic in Python and asserts the behaviours the
vault-not-in-runners split depends on:
  * same value -> same token (equality-preserving, deterministic)
  * distinct values -> distinct tokens
  * findings count every structured-PII hit by type
  * rehydrate round-trips a token back to the original value
  * an unknown token passes through unchanged (loud, never silently wrong)
Keep PATTERNS in sync with scrubber/server.js; tests/unit/scrubber.test.mjs is the
full node:test suite over the real module.

    python tests/scrub_test.py     # exits non-zero on any failure
"""
import re, sys

# --- mirror of scrubber/server.js PATTERNS (ordered: card before phone) ---
PATTERNS = [
    ("CARD",  re.compile(r"\b\d(?:[ -]?\d){12,15}\b")),
    ("SSN",   re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
]


class Vault:
    """Mirror of the per-scope deterministic token vault."""
    def __init__(self):
        self.by_value = {}
        self.by_token = {}
        self.counters = {}

    def scrub(self, text):
        findings = {}
        out = text or ""
        for typ, rx in PATTERNS:
            def repl(m, typ=typ):
                key = f"{typ}:{m.group(0)}"
                token = self.by_value.get(key)
                if not token:
                    self.counters[typ] = self.counters.get(typ, 0) + 1
                    token = f"«{typ}_{self.counters[typ]}»"
                    self.by_value[key] = token
                    self.by_token[token] = m.group(0)
                findings[typ] = findings.get(typ, 0) + 1
                return token
            out = rx.sub(repl, out)
        return out, findings

    def rehydrate(self, text):
        restored = [0]
        def repl(m):
            t = m.group(0)
            if t in self.by_token:
                restored[0] += 1
                return self.by_token[t]
            return t
        out = re.sub(r"«[A-Z]+_\d+»", repl, text or "")
        return out, restored[0]


fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


print("scrub: deterministic, equality-preserving tokenization:")
v = Vault()
out, findings = v.scrub("Email ada@lab.io or ada@lab.io again")
tokens = re.findall(r"«EMAIL_\d+»", out)
check("same value -> two identical tokens", len(tokens) == 2 and tokens[0] == tokens[1], str(tokens))
check("EMAIL counted twice", findings.get("EMAIL") == 2)

out2, _ = v.scrub("later, ada@lab.io again")
check("same value keeps its token across calls", re.findall(r"«EMAIL_\d+»", out2)[0] == tokens[0])

out3, _ = v.scrub("a different one: grace@lab.io")
new_token = re.findall(r"«EMAIL_\d+»", out3)[0]
check("distinct value -> distinct token", new_token != tokens[0], new_token)

print("scrub: counts each structured-PII type:")
v2 = Vault()
_, f2 = v2.scrub("Call 555-123-4567, SSN 123-45-6789, mail ivy@lab.io")
check("PHONE counted", f2.get("PHONE") == 1, str(f2))
check("SSN counted", f2.get("SSN") == 1, str(f2))
check("EMAIL counted", f2.get("EMAIL") == 1, str(f2))

print("scrub: benign text untouched:")
out4, f4 = v2.scrub("The quarterly report is ready.")
check("no findings on benign text", f4 == {})
check("benign text unchanged", out4 == "The quarterly report is ready.")

print("rehydrate: round-trip + unknown-token passthrough:")
v3 = Vault()
scrubbed, _ = v3.scrub("contact mallory@lab.io")
restored_text, n = v3.rehydrate(f"please {scrubbed}")
check("round-trips to the original value", restored_text == "please contact mallory@lab.io")
check("restored count is 1", n == 1)

unknown, n0 = v3.rehydrate("see «EMAIL_9999» and «SSN_42»")
check("unknown token passes through unchanged", unknown == "see «EMAIL_9999» and «SSN_42»")
check("nothing restored for unknown tokens", n0 == 0)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("All scrub/rehydrate (python-mirror) checks passed.")
