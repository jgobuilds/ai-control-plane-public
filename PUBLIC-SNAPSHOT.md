# This is a published snapshot

A point-in-time copy of a repository that is developed privately, exported with
`git archive` so it carries **no git history** — only the tracked files as they
stood at export.

Two consequences worth knowing:

- **No commit history, no blame, no tags.** That is deliberate: history is the
  usual way private material leaks into a newly-public repo, and dropping it
  removes the whole class of risk rather than auditing for it.
- **It does not update itself.** A newer snapshot replaces this one wholesale.

Exported 2026-07-26. Issues and pull requests are welcome here; see
CONTRIBUTING.md where present.
