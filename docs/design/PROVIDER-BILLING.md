# Provider billing — where a subscription covers agent work, and where it doesn't

**What this is for.** This stack's economics rest on running vendor CLIs against
whatever entitlement the operator already holds, rather than metered API tokens.
Whether that is *permitted* is a per-vendor, per-auth-path question, it has
changed twice in 2026, and it changed in both directions. This file records what
each vendor says, when, and with what confidence — so the architecture rests on a
dated citation instead of a memory.

> **Not legal advice, and not a substitute for the vendor's current terms.**
> Every row below is a reading of public sources on the date given. Terms change
> without notice to us. **Verify before relying on any row for a commercial
> decision**, especially before putting client work through a path.

## The short version

| Provider | Auth path | Billing | Headless / unattended agent use | Confidence |
|---|---|---|---|---|
| **Anthropic** | Claude subscription (Pro / Max) via `CLAUDE_CODE_OAUTH_TOKEN`, driving `claude -p` | **Agent SDK monthly credit**, then metered | **Yes, but capped and separate.** Since **2026-06-15** `claude -p` no longer draws on plan usage limits at all — it draws on a per-user monthly Agent SDK credit ($20 Pro / $100 Max 5x / $200 Max 20x). Must be **claimed once**. When it runs out, requests **STOP** unless usage credits are enabled. | **High** — primary source, quoted below |
| **Anthropic** | Claude subscription, *interactive* Claude Code / Claude.ai | Flat subscription limits | n/a — interactive only, and explicitly **not** what this stack does | High |
| **Anthropic** | Subscription OAuth used by a **third-party** app/SDK | — | **No.** A Feb 2026 policy restricts subscription OAuth to Claude Code and Claude.ai contexts. | Medium — reported as a docs cleanup by some sources |
| **Anthropic** | API key (Console / Bedrock / Vertex) | Metered | Yes, unrestricted | High |
| **Google** | Personal Google login → free Gemini Code Assist licence | Free tier, 60 req/min · 1,000 req/day | Yes within quota; quota is the practical ceiling, not a permission question | Medium |
| **Google** | AI Studio API key (`GEMINI_API_KEY`) | Free tier, then metered | Yes | Medium |
| **Google** | Code Assist Standard / Enterprise seat | Per-seat subscription | Yes, with higher quotas + SLA + admin controls | Medium |
| **Google** | Vertex AI | Metered, GCP billing | Yes — the enterprise/production path | High |
| **OpenAI** | ChatGPT sign-in (Free → Pro) for Codex CLI | Plan-included credits | Included on every plan, but OpenAI's own guidance points **programmatic / CI / automation** use at API keys | Medium |
| **OpenAI** | API key | Metered | Yes — the documented path for automation | High |
| **Local** (Ollama, llama.cpp) | none | Hardware only | Yes, no terms question at all | High |

## Anthropic — and this file was WRONG about it until 2026-08-10

This is the path the stack actually uses (`claude -p` inside `claude-runner`,
authenticated by `CLAUDE_CODE_OAUTH_TOKEN`), so the detail matters — and the
detail was backwards here for seven weeks.

**What this file said:** the metered credit split was announced 2026-05-14 and
*"cancelled 2026-06-15 before taking effect"*, so `claude -p` still drew on
subscription limits.

**What Anthropic's Help Center says.** 2026-06-15 is the date it **started**.
Verbatim, from the article on using the Agent SDK with a Claude plan
(`support.claude.com/en/articles/15036540-...`, page **last modified
2026-06-16**, fetched 2026-08-10):

> "Starting June 15, 2026 , Claude Agent SDK and claude -p usage no longer counts
> toward your Claude plan's usage limits. Your subscription usage limits stay the
> same and stay reserved for interactive use of Claude Code, Claude Cowork, and
> Claude."

> "Claude subscription plans are now eligible to receive a monthly Agent SDK
> credit. This credit covers Claude Agent SDK usage, the claude -p command, and
> third-party apps built on the Agent SDK."

So the change did not merely happen — it landed in essentially the form
originally announced.

### What that actually means for this stack

| | |
|---|---|
| What `claude -p` bills against | the **Agent SDK monthly credit**, not plan usage limits |
| Credit by plan | Pro **$20** · Max 5x **$100** · Max 20x **$200** · Team Standard $20 / Premium $100 · Enterprise usage-based $20 / seat-based Premium $200 |
| Seat-based Enterprise **Standard** seats | *"aren't eligible to claim the Agent SDK monthly credit"* |
| Claiming it | *"One-time opt-in. You claim your credit through your Claude account once."* |
| Rollover / pooling | none — *"Unused credits don't roll over"*, *"can't be shared or pooled across teammates"* |
| When it runs out | *"additional Agent SDK usage flows to usage credits at standard API rates—but only if you've enabled usage credits. **If usage credits aren't enabled, Agent SDK requests stop until your credit refreshes.**"* |
| Also covered | the Claude Code GitHub Actions integration; third-party apps authenticating via the Agent SDK |
| Not covered | interactive Claude Code, Claude web/desktop/mobile, Claude Cowork |

**Two operational consequences, neither of them theoretical.** Unattended agent
volume now has a hard monthly ceiling per user, denominated in dollars at standard
API rates — so capacity planning is a spend question, not a rate-limit question.
And the failure mode at the ceiling is a **stop**, not a slowdown, unless usage
credits are enabled: worth knowing before a lane discovers it at 3am.

**Open, and not assumed either way:** whether this operator has claimed the
credit, and whether usage credits are enabled as the overflow. Both are account
settings this file cannot read.

### Third-party subscription auth, 2026-02

Subscription OAuth tokens were reported as limited to "Claude Code and Claude.ai"
contexts, with third-party products directed to API keys
([alternativeto](https://alternativeto.net/news/2026/2/anthropic-officially-bans-using-subscription-authentication-for-third-party-claude-use),
2026-02-20 — that source flags confusion about scope). **Confidence: low, and now
partly superseded** — the Agent SDK article above says the credit explicitly
covers *"Third-party apps that authenticate with your Claude subscription through
the Agent SDK"*, which reads as permitting the thing the February reporting
described as banned. Not re-verified against a primary source; treat as unsettled.

### How this file got it wrong, because that is the more useful lesson

The claim rested on three secondary sources and **no primary one**. Re-fetching
the blog that anchored it shows the fault was not a mangled summary — the source
itself says, at line 49 of its own text:

> "Update · June 15, 2026 · This change was paused … it is not taking effect."

The summarizer relayed that faithfully. **The blog was simply wrong**, and
Anthropic's own article — both primary *and* a day newer, modified 2026-06-16 —
contradicts it. Three blogs agreeing with each other is one source, not three.

That is a different failure from the one this file's tooling was worried about,
and the more common one: not fabrication, but faithful transmission of a
confident secondary source nobody checked against the vendor. The fix is not only
"read the raw page" — it is **cite the primary source, and date it**. Hence
`scripts/fetch_source.py` and the research rule in `AGENTS.md`.

## Reading the market claim honestly

The dossier previously recorded "flat-subscription CLI billing" as a
**differentiator**, then as **verified dead** after the May announcement. Both
were wrong at the time they were written, in opposite directions, and the second
was written from July sources describing a change that had been cancelled in
June.

The accurate statement is neither — and as of 2026-08-10 it is neither of those
*and* not the third thing this file claimed in between:

> **Unattended agent work on a Claude subscription is real, capped, and
> metered.** Since 2026-06-15 it draws on a per-user monthly Agent SDK credit at
> standard API rates, not on plan usage limits. That is a cost line with a
> ceiling, not a free entitlement, and it is certainly not a moat.

Anything outward-facing should say that, not "differentiator #1" and not
"retired". A claim that flips with each vendor announcement was never load-
bearing enough to lead with.

## Sources, and how to re-check them

Every row above that is marked **High** was read from a raw fetch of the vendor's
own page, saved to `research/raw/` and quoted by line. Re-run it:

```bash
python scripts/fetch_source.py get https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan --name anthropic-support-agent-sdk
python scripts/fetch_source.py quote anthropic-support-agent-sdk "claude -p"
```

| Slug | Source | Page last modified | Read on |
|---|---|---|---|
| `anthropic-support-agent-sdk` | support.claude.com/en/articles/15036540 | 2026-06-16 | 2026-08-10 |
| `anthropic-support-pro-max` | support.claude.com/en/articles/11145838 | — | 2026-08-10 |
| `anthropic-support-usage-credits` | support.claude.com/en/articles/12429409 | — | 2026-08-10 |
| `secondary-digitalapplied` | digitalapplied.com blog | 2026-06-16 | 2026-08-10 — **contradicted by the primary source; retained as evidence of the error** |

Google and OpenAI rows are **not** primary-verified and are marked Medium
accordingly. They are the next thing to check, and until then they carry the same
risk the Anthropic row just realised.

## Maintaining this file

Re-check on any of: a vendor billing announcement, a runner failing with a
quota/entitlement error, or **every six months** — whichever is first. Update the
date and the confidence column, and record what changed rather than overwriting
it: this file's whole value is that it shows the claim moving — including the
2026-08-10 correction, where it moved because it had been wrong.
