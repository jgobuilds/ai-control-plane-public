# Contributing

Thanks for considering it. One thing to read before you write code, because it
is unusual and refusing is a legitimate choice.

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
welcome. Open an issue describing the change instead of a pull request. A clear
bug report or design proposal is often worth more than the patch, and it costs
you nothing.

## Before you open a pull request

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
