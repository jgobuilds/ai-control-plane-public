// CANONICAL PII detection patterns. VENDORED BYTE-IDENTICAL into every service
// that must make the same decision: router/, claude-runner/, gemini-runner/.
//
// WHY A COPY AND NOT AN IMPORT. Each service is its own Docker build context, so
// a shared parent path is not reachable at build time. The house pattern is a
// byte-identical vendored copy with a drift test — `tests/pii_vendor_test.py`
// fails if any copy differs, because two services disagreeing about what counts
// as PII is worse than either rule alone: the router would refuse what a runner
// accepts, and the gap would only show up as an inconsistency nobody reproduces.
//
// EDIT THE ROUTER COPY, then re-vendor. Never patch a runner's copy in place.
//
// This is a regex BASELINE, not a classifier. `change-safety.md` is explicit:
// "a heuristic without a labeled corpus is unshipped", and the corpus lives in
// tests/. Names and most addresses are not reliably detectable by regex at all,
// so absence of findings is not proof of absence — which is why tokenization at
// ingest, not detection at the edge, remains the primary control.

const PII_PATTERNS = [
  { type: "email", re: /[\w.+-]+@[\w-]+\.[\w.-]+/g },
  { type: "ssn", re: /\b\d{3}-\d{2}-\d{4}\b/g },
  { type: "phone", re: /\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b/g },
  { type: "card", re: /\b\d(?:[ -]?\d){12,15}\b/g },
];

function piiScan(text) {
  const hits = [];
  for (const { type, re } of PII_PATTERNS) {
    // A /g regex carries lastIndex state across calls when reused with .test();
    // .match() on a fresh string does not, which is why this uses match. Left
    // explicit because "it worked in the router" is not a reason it is safe here.
    const m = String(text || "").match(re);
    if (m) hits.push({ type, count: m.length });
  }
  return hits;
}

module.exports = { PII_PATTERNS, piiScan };
