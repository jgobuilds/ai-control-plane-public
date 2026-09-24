// Deterministic rules — Tier 0, evaluated BEFORE any model is touched.
// This is where "prefer rules-based / deterministic where viable" lives.
// Each rule: { name, match(input) -> boolean, run(input) -> result }.
// First matching rule wins; its result is returned with zero LLM cost.
//
// `input` = { prompt, task_type, payload, ... } (whatever n8n sends).
// Add your own high-frequency, well-defined cases here. Examples below.

module.exports = [
  {
    name: "healthcheck",
    match: (i) => /^\s*(ping|health)\s*$/i.test(i.prompt || ""),
    run: () => ({ text: "pong" }),
  },

  {
    name: "email-extract",
    // Pulling an email out of text is a regex job, not a model job.
    match: (i) => i.task_type === "extract" && i.extract === "email",
    run: (i) => {
      const m = (i.prompt || "").match(/[\w.+-]+@[\w-]+\.[\w.-]+/);
      return { text: m ? m[0] : null };
    },
  },

  {
    name: "keyword-category",
    // Deterministic triage by keyword — cheaper and more predictable than a
    // classifier when the vocabulary is known. Falls through if no keyword hits.
    match: (i) =>
      i.task_type === "classify" &&
      /\b(refund|invoice|password|cancel)\b/i.test(i.prompt || ""),
    run: (i) => {
      const p = (i.prompt || "").toLowerCase();
      const label = p.includes("refund") || p.includes("invoice")
        ? "billing"
        : p.includes("password")
        ? "account"
        : "support";
      return { text: label };
    },
  },
];
