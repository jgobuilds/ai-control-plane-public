---
id: SOL-20260727-n8n-schedule-cli-activate
title: An n8n workflow read as active and never fired
status: solved
last_verified: 2026-07-27
tags: [n8n, operations]
signature: none
---

# An n8n workflow read as active and never fired

## What happened

`aicp-dispatch` was activated with `n8n update:workflow --active=true`. `n8n list:workflow --active=true` listed it, and n8n's own database showed `active = 1`. Fifteen minutes and forty polls later it had produced **zero executions** on a ten-minute schedule.

## Why it happened

A CLI activation writes the database row but does **not** register the Schedule Trigger with the *running* process. The workflow reads as active from every angle available to a person checking, and the scheduler has never heard of it.

This is the same class as the webhook-registration lesson already recorded in ADR 0006 — n8n's runtime state and its stored state are separate things, and the CLI writes only one of them.

## What was tried that did NOT work

- **Trusted the `active` flag.** It was correct and irrelevant: it proves the row, not the registration.
- **Read n8n's SQLite without copying the `-wal` file.** Returned stale rows — the first read reported the workflow did not exist at all. Any read of a live SQLite database needs the write-ahead log copied alongside it.
- **Assumed the schedule config was malformed.** It was not; `n8n` had stored `{"field": "minutes", "minutesInterval": 10}` exactly as written, and `triggerCount` was 1.

## The fix

`docker compose restart n8n`. Executions began on the next tick.

## Prevention

`scripts/n8n_bootstrap.py --activate` now prints the required restart instead of a bare ✓. The tool that creates the false impression is the one that has to correct it — a note in a runbook would not have been read at the moment it mattered.

## How we would know it came back

Check the **execution count**, never the active flag. `select count(*) from execution_entity where workflowId=…` after one interval has elapsed. Activated and running are different facts.
