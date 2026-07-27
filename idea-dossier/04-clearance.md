# 04 — Clearance (source & licence register)

**Not legal advice.** This surfaces the real questions for counsel; the money +
copyleft-adjacent findings below must be confirmed by a lawyer before any
commercial move or "open source" claim.

The load-bearing finding of the whole vet lives here.

| Source | What it gives | Licence | Obligation | Commercial OK? | Verdict |
|---|---|---|---|---|---|
| **n8n** | Orchestration runtime (the whole trigger/schedule/gate/glue layer) | **Sustainable Use License (fair-code, NOT OSI open source)** | Internal use / client work fine. **Hosting-for-others prohibited; selling a product/service whose value substantially depends on n8n prohibited; OEM-embedding needs an n8n partnership** | **No — not without an n8n commercial agreement** | **BLOCKER for the product** |
| LiteLLM | Router / budget (if adopted) | MIT | Attribution | Yes | Clear — **adopt** rather than rebuild the router |
| Langfuse | Tracing / eval (if adopted) | MIT core; enterprise modules commercial | Attribution; don't self-host the commercial modules unlicensed | Yes (core) | Clear — adopt the core |
| Docker / Alpine base images | Runtime | Apache-2.0 / permissive | Notice | Yes | Clear |
| claude / gemini CLIs | The agents themselves | Vendor ToS + subscription/credit terms | Follow ToS; **subscription auth for automation is now metered** (`03-viability.md`) | Per ToS | Clear to use; not a cost moat |
| brightworks' own code | Everything in this repo | currently unlicensed (private) | — | choose deliberately | **Needs a LICENSE decision** |

## The n8n problem, stated plainly

Brightworks is **built on n8n as its core runtime.** From n8n's own docs
([Sustainable Use License](https://docs.n8n.io/sustainable-use-license/),
[announcement](https://blog.n8n.io/announcing-new-sustainable-use-license/)):

- *"Selling a product, service or module whose value substantially depends on
  n8n"* is a **prohibited use.**
- **OEM-embedding** n8n in a commercial product *"requires a partnership with n8n,
  usually involving a revenue-share model or a significant annual license fee."*

Consequences:

1. **You cannot call the bundled product "open source."** n8n is fair-code, not
   OSI-approved. Marketing the whole as "Open …" would be inaccurate and, given
   it also touches the trademark/branding of an OSI term, reputationally risky.
2. **A supported/commercial edition is not permissible** while n8n is the core,
   absent an n8n commercial agreement. That negotiation (revenue share or annual
   fee) is the *first* gate on the entire open-core plan, not a detail.
3. **The escape hatch is to drop n8n from the core** and drive the lanes from
   something permissively licensed (a small scheduler + the `lanes` HTTP service
   you already built). That is a real re-architecture, and it removes the thing
   that made setup approachable.

## Questions for counsel

- Does our specific use (n8n as the orchestration substrate a commercial edition
  is sold around) fall under "substantially depends on n8n"? (Reading suggests
  **yes**.)
- If we pursue commercialization, what does an n8n OEM/embedding agreement cost?
- Licence choice for our own code (Apache-2.0 vs AGPL vs a source-available BSL),
  and the **trademark** registration that is the actual moat — draft in an ADR,
  confirm with counsel before filing.
