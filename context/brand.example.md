# Branding (example — the engine ships neutral)

This engine is **brand-neutral by default** so any org can run it under its own
identity. Nothing here carries a specific org's palette — that's deliberate.

To brand generated output (HTML, dashboards, charts, docs), pick one:

- **Private overlay (recommended).** Provide a sibling `<org>-instance` directory
  (e.g. `../acme-instance`), or set `AICP_INSTANCE` to its path; onboarding loads
  its `brand/` first. Real values never enter this repo.
- **Local override.** Copy `brand-tokens.example.css` → `brand-tokens.css` (the
  real name is gitignored) and edit it with your tokens.

## Placeholder palette (obviously generic — replace it)

```
primary  #4F46E5     accent   #F59E0B
success  #16A34A     danger   #DC2626
ink      #1F2937     muted    #6B7280     line #E5E7EB     bg #FFFFFF
headings + body: system-ui
```

## Rules for any generated visual output (org-agnostic — keep these)

Use tokens, never raw hex. Charts follow one fixed categorical order. Meet WCAG
AA contrast and keep the palette colorblind-safe (never color as the only
signal). Support light and dark. One accent job per color; generous white space.
Full method: the `brand` lens in the `ai-standards` overlay.
