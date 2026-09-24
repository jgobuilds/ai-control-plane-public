# Transcribe tenant calls with Google Meet, not locally — speaker identity is the requirement, and Google is already the processor

**Date:** 2026-07-27 · **Lens:** `ai-standards/references/build-vs-adopt.md`,
`cost-awareness.md`, `dependency-security.md`,
`ai-control-plane/docs/security/RETENTION.md`, ADR
[0010](0010-agent-work-management.md) (no external processor for tenant work)

Records a **reversal**. The first recommendation in conversation was to
transcribe locally, on the reasoning that a hosted transcriber is an external
processor. That reasoning was too blunt, and one requirement inverted it.

## The question

Automate transcription of tenant calls, store and process it in this system, and
**group what was said by stakeholder and by department**.

That last clause is not a nice-to-have. It is what makes a transcript useful for
a data-maturity engagement — *"the platform team says X, the governance owner
says the opposite"* is the finding. A wall of undifferentiated text is not.

## Considered

Facts from this repo (`drive-sync.workflow.json`, `scrubber/`,
`context/scopes.json`, `data-handling.md`) and from the known behaviour of the
tools. **Not verified:** whether the current Workspace tier includes
transcription — see Status.

| Option | Cost | Verdict |
|---|---|---|
| **A. Google Meet built-in transcription → Drive → `drive-sync` → scrubber** | **$0 marginal** — included in a Workspace seat already paid for. Cost *trigger*: needing a higher Workspace tier if the current one excludes it. No new vendor, no new contract | **Adopt** — it is the only option that yields real speaker **identity**, and it adds no processor we do not already have a DPA with |
| **B. Local Whisper (`whisper.cpp` / `faster-whisper`)** | $0 licence (MIT/Apache). **Fixed** — a GPU, or long CPU runtimes per hour of audio. Non-dollar: **Whisper does not diarize at all**, so this needs a second model (`pyannote.audio`, HuggingFace token + licence acceptance) which yields `SPEAKER_00`, not a person | **Defer — keep as the documented fallback.** Right when a contract forbids Google, or the call is in person with no Meet. Wrong as the default, because it cannot answer the question being asked |
| **C. Hosted third-party transcription** (Deepgram, AssemblyAI, Otter) | **Metered** per audio-minute. Non-dollar: **a new processor holding raw tenant audio** | **Reject** — this is the option ADR 0010's no-external-processor stance exists to refuse. Good diarization does not buy back a new party in the data path |
| **D. Meet transcription *plus* Gemini-in-Workspace summarisation** ("take notes for me") | $0 marginal if included; possibly a separate add-on | **Defer — verify separately.** These are distinct admin toggles and may carry distinct terms. Assuming the Workspace DPA covers everything under the Meet menu is exactly the assumption that produced the Gemini finding below |
| **E. Do nothing — take notes by hand** | $0; **high** non-dollar — the analysis this enables is a differentiator of the engagement | **Reject as the end state**, but it is the correct interim while consent and retention (below) are unresolved |

## Chose

1. **Google Meet transcription is the default path.** Meet → Drive →
   `drive-sync` → scrubber → `workspace/scopes/<tenant>/<engagement>/ingest/`.
   That pipeline already exists and has never carried real data; this is its
   first real use, which is worth knowing rather than assuming.
2. **Local Whisper + diarization is the documented fallback**, not the default.
   It is the answer when a tenant contract forbids Google or there is no Meet.
3. **Never a third-party transcriber for tenant audio.** Reject, not defer.
4. **Consent is settled in the engagement contract before any of this runs.**
   Connecticut is one-party, but the binding rule is the *tenant's* jurisdiction
   and their own policy — an EU tenant changes the answer entirely. This is a
   clause, not a feature flag.
5. **Retention must be real before audio lands.** The retention lane is
   **dry-run only** today: no vault access, no deletions. Call recordings are the
   highest-risk artifact we would hold, and holding them under a sweep that
   deletes nothing is worse than not holding them.
6. **The model never sees names.** The scrubber tokenizes before the transcript
   reaches a runner and rehydrates after. The vault is not mounted into a runner.
7. **Fix the Gemini auth path first** — see Because.

## Because

**The requirement decided it, and the requirement is identity, not separation.**
Whisper does not do speaker diarization. Bolting on `pyannote` produces
`SPEAKER_00` / `SPEAKER_01` — *acoustic clusters*, not people — and mapping those
to names needs voice enrollment or manual labelling **per call**. Meet does not
diarize because it does not have to: it knows which participant owns which audio
stream, so the transcript carries a real identity that joins to the Workspace
directory. **You cannot group comments by department from `SPEAKER_01`.** One
path makes the requested feature native; the other makes it a research project
with a per-call chore attached.

**The reversal, stated plainly.** The earlier recommendation applied
"hosted = external processor" uniformly. The correct distinction is narrower:

> **A NEW processor, or one already under contract?**

Google is already an approved processor in `data-handling.md`. Meet transcription
keeps tenant audio inside a Workspace boundary already covered by a DPA — it adds
no party. Deepgram would add one. The original reasoning was right about option C
and wrong about option A, and applying it uniformly would have bought a worse
product for no privacy gain.

**This is the second finding pointing at the same confusion.** The
`gemini-runner` authenticates with a **free AI Studio API key** (verified:
`GEMINI_API_KEY` set, `.gemini/projects.json` empty — no Workspace login), while
`data-handling.md` records "Google" as an approved processor on the strength of a
Workspace relationship. **Same vendor, two entirely different sets of terms**, and
every document reads as compliant. Transcription makes that gap material: it is
tolerable for a code question and not for a tenant call.

**What this gives up, stated plainly.** A dependency on Google for a
tenant-facing capability, in a system whose pitch is that it does not depend on
anyone. That is a real tension and the honest defence is narrow: we already
depend on Google here, under a DPA, and option B remains built and documented so
the dependency is *chosen* rather than structural. If that stops being true —
Meet transcription degrades, gets repriced, or a tenant forbids it — the fallback
is a configuration change, not a redesign.

## Status

**Decided, not built.** Verified from this repo: `drive-sync` exists and
tokenizes into a scoped ingest directory; the scrubber holds the vault and it is
not mounted into runners; the retention lane is dry-run only; the Gemini runner
uses a free AI Studio key.

**Not verified, and blocking:**

- **Whether the current Workspace tier includes Meet transcription.** Historically
  a Business Standard-and-above feature; packaging changes. Check the admin
  console before building anything.
- **Whether Gemini-in-Workspace note-taking carries the same terms** as plain
  transcription. Separate toggles, possibly separate terms.
- **`drive-sync` has never run** — it needs OAuth and is listed as such.

**Revisit when:**

- **A tenant contract forbids Google** — switch to option B; that is what it is
  for, and the trigger is contractual rather than technical.
- **Meet's speaker attribution proves insufficient** for department grouping —
  likely the first real failure, since it gives a display name and the join to
  org units is ours to build.
- **Retention stops being dry-run** — that gate must clear before the first
  recording, not after.
- **Transcript volume makes Drive the wrong store** — this is a per-engagement
  trickle today, and that assumption should be revisited rather than inherited.
