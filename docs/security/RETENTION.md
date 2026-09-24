# Retention & right-to-deletion

Two Python scripts turn `scopes.json` retention policy into enforced reality and
give a provable tenant-offboarding story. Both are **stdlib-only**, deterministic,
and safe to run on the Windows host (`python` is available; Node is not needed).

- `scripts/retention_sweep.py` — age out work-product per scope's `retentionDays`.
- `scripts/offboard_scope.py` — delete a scope's whole footprint with a signed-in-
  evidence deletion certificate.

Neither script ever touches the audit ledger (`audit/decisions.jsonl`).

---

## Retention sweep

Every scope has an **effective retention window**: a node's
`controls.retentionDays` overrides `levelDefaults[<level>].retentionDays`; `null`
means *keep forever*. In the shipped config only the `personal` level defaults to
90 days — everything else keeps until explicitly offboarded.

The sweep walks `workspace/scopes/<chain>/{ingest,deliverables}` (recursively —
including `deliverables/sent/`) and, for each scope with a window, selects files
whose mtime is **older than** `retentionDays`.

```
python scripts/retention_sweep.py            # DRY-RUN — lists what WOULD go
python scripts/retention_sweep.py --apply     # actually deletes + prunes vault
python scripts/retention_sweep.py --scope jane --apply
```

Dry-run is the default and changes nothing. The report shows, per scope: the
retention window, each candidate file with its age and size, the byte total, and
the vault-prune result.

### Vault pruning (heuristic, conservative)

When files leave, the tokens they referenced may become orphaned in the scope's
`vault/<scope>.json` map. The sweep flags a token as **prunable only when it can
prove the token is orphaned** — i.e. *neither* the token string (`«EMAIL_1»`)
*nor* its underlying raw value appears in **any** file that would remain under the
scope.

This is deliberately over-cautious, for one reason: **a token that still appears
anywhere is never pruned**, so rehydration of any surviving deliverable can never
silently lose its mapping. The caveat cuts the other way too — because matching is
by substring, the prune is *best-effort*: it will keep an entry it isn't certain
is dead rather than risk deleting a live one. Under `--apply`, pruned entries are
removed from `byToken` and `byValue`, but `counters` is left intact so future
tokenization keeps assigning deterministic, non-colliding token numbers.

> Silo scopes (e.g. `tenant-a`, `tenant-b`) keep their workspace + vault on their
> **dedicated runner**, not in the shared tree — run the sweep there, pointed at
> that runner's root. The script sweeps whatever `--root` it is given and treats
> missing directories as empty, so it is safe to run anywhere.

---

## Provable offboarding

`offboard_scope.py` is the right-to-deletion / tenant-exit tool. It removes:

- the scope's workspace subtree `workspace/scopes/<chain>/`, and
- its token vault `vault/<scope>.json`,

and writes a **deletion certificate** to
`audit/deletions/<scope>-<utc-timestamp>.json`.

```
python scripts/offboard_scope.py tenant-a                       # DRY-RUN
python scripts/offboard_scope.py tenant-a --confirm --operator jane
python scripts/offboard_scope.py consulting --recursive --confirm
```

- **Dry-run by default.** It prints the full manifest (every file, its size, its
  SHA-256) and where the certificate *would* go, but deletes nothing and writes no
  certificate. `--confirm` executes.
- **Leaves first.** It refuses a scope that still has child scopes unless
  `--recursive` is given, in which case the scope *and every descendant* (their
  vault files included) are removed and all appear in one certificate.
- **Operator** comes from `--operator`, else `$USER` / `$USERNAME`, else
  `unknown`.

### The certificate

The manifest's sizes and SHA-256 hashes are computed **before** anything is
deleted, so the certificate is a verifiable record of exactly what was destroyed —
hand it to the departing tenant as proof, or keep it for a DSAR/audit response.
It contains: `scope`, `chain`, `operator`, UTC `ts`, `scopesPurged`, the
`workspaceSubtree` + `vaultFiles` paths, `fileCount`/`totalBytes`, and the
per-file `manifest` (`path`, `size`, `sha256`).

Because `audit/` is git-ignored (see below), certificates live on the **deploy
volume** as runtime compliance evidence — they are retained, not committed. Ship
`audit/deletions/` to WORM / append-only object storage alongside the decision
ledger for non-repudiation.

---

## Scheduling the sweep

The sweep is a plain command, so schedule it however you run cron-like work:

- **n8n** — `n8n-workflows/retention-sweep.workflow.json` (Schedule Trigger →
  Execute Command running `retention_sweep.py --apply`). It ships **inactive**;
  review the dry-run output first, then set `active` and enable it.
- **cron** (Linux deploy): `15 3 * * * cd /app && python scripts/retention_sweep.py --apply >> /var/log/retention.log 2>&1`
- **Task Scheduler** (Windows host): a daily task running the same command.

Offboarding is intentionally **not** scheduled — it is a deliberate, human-invoked
action (a tenant leaving), always run with `--confirm` by an operator.

---

## Tests

`python tests/retention_test.py` builds a throwaway repo layout, backdates files
with `os.utime`, and asserts: old files selected / new files kept; dry-run is
inert; `--apply` deletes exactly the aged files; vault prune is conservative
(referenced tokens survive, orphaned tokens are pruned only on apply); offboarding
refuses non-leaves without `--recursive`, dry-run writes no certificate, and
`--confirm` removes the subtree + vault and writes a certificate whose manifest
hashes match the pre-deletion files.
