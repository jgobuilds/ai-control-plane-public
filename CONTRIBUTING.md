# Contributing

Thanks for considering it. Two things to read before you write code — the first
because it changes *where* to send work, the second because it is an unusual ask
and refusing it is a legitimate choice.

## If you are reading this on the public repository, it is a snapshot

`*-public` is a **single-commit snapshot**, not the working history. Development
happens in a private repo; a publish script copies the tracked tree minus an
explicit exclusion list, runs the hygiene, PII and link gates *against the
snapshot*, and force-pushes one orphan commit.

**A pull request there cannot be merged.** Every publish replaces the commit, so
a branch you fork today shares no ancestor with the one that exists tomorrow.
That is a property of the repo, not a judgement about the patch.

So: **open an issue**. Describe the change, or paste the diff, or link a fork —
it gets applied upstream with attribution and appears in the next snapshot.
Clumsier than a PR, and the honest shape given the above. Say so if you would
rather not be credited by name; the default is to credit you.

One thing worth knowing before filing: **the known gaps are already tracked.**
`drive-sync` has never run, the retention sweep deletes nothing, and the
`dispatch` lane has never carried real work — all three are open issues and the
README says so. A gap that *isn't* on that list is very welcome.

And nothing published here arrived by accident: publication is an allowlist the
script reads, never a consequence of where a file was saved. If something looks
left in by mistake, that is worth an issue too.

## Contributions require a copyright assignment

By submitting a contribution you **assign copyright in it to Brightside Data
LLC**, and in return receive a perpetual, worldwide, royalty-free licence back to
use your own contribution for any purpose, including under any other licence.
You keep full use of your own work; the project gains a single, unambiguous
rights holder.

Add this line to each commit, using the same name and email as your commit
author:

```
Copyright-Assignment: I assign copyright in this contribution to Brightside Data
LLC, and confirm I have the right to do so.  <your name> <your@email>
```

If your employer owns your work output, you need their sign-off before that line
is true. Please sort that out first rather than after review.

## Why — stated plainly, because it is a real ask

This project is AGPL-3.0. AGPL is unworkable for some organisations, so a
commercial licence on different terms is available on request. **Offering that
depends on one entity holding all the copyright.** Merge a single contribution
without assignment and the project can never be relicensed again — not by
agreement, not by paying, not ever, short of tracking down every contributor.

That is not hypothetical: it is why several well-known projects are permanently
stuck on a licence they have outgrown. The assignment is collected at the door
because it cannot be collected retroactively.

**What you get in exchange, in writing:** the licence-back above means you may
keep using and relicensing your own contribution however you like — including in
proprietary work. Assignment here removes a constraint on the project, not on
you.

**If you would rather not assign**, that is entirely reasonable and you are still
welcome. Open an issue describing the change instead. A clear bug report or
design proposal is often worth more than the patch, and it costs you nothing —
and on the public snapshot an issue is the channel regardless.

**The assignment applies to code, not to issues.** Reporting a bug, arguing with
an ADR, or pointing out that a cost column is wrong requires nothing from you.

## Before you send a change — by PR upstream, or by issue here

- **Read [`AGENTS.md`](AGENTS.md)** — it is the source of truth for how this repo
  is built, and the tool-specific files are generated pointers to it.
- **This repo ships brand-neutral.** No org names, palettes, or marketing. The
  pre-commit guard (`scripts/check_public_hygiene.py`) enforces it; run it.
- **Do not hand-edit generated files.** `ARCHITECTURE.md`, the compliance map and
  the use-case register are all built by scripts in `scripts/`. Edit the source
  and regenerate.
- **Architectural changes need an ADR** in `docs/decisions/`, using the template,
  including the Cost column. `scripts/adr_check.py` enforces the shape.
- **Never weaken a gate to make CI pass.** If a guard is wrong, change it
  deliberately in its own commit and say why.
- **No secrets, ever** — including in commit messages, which are permanent and
  public. The PII gate scans both the diff and the message.

## Security

Please do not open a public issue for a vulnerability. See
[`docs/security/THREAT-MODEL.md`](docs/security/THREAT-MODEL.md) for scope and
what is already known, and report privately.
