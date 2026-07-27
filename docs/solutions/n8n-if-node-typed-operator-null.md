---
id: SOL-20260727-n8n-if-typed-null
title: A lane errored on its own healthy state
status: solved
last_verified: 2026-07-27
tags: [n8n, operations]
signature: is a string but was expecting a number
---

# A lane errored on its own healthy state

## What happened

The dispatch lane failed with `Wrong type: '' is a string but was expecting a number [condition 0, item 0]` on the IF node named "Was anything claimed?" — the node whose entire job is to detect that nothing was claimed.

## Why it happened

The condition used a **typed operator** — type `number`, operation `exists` — against `dispatched`, which is `null` when the queue is empty. n8n's strict type validation rejects the value *before* the operation runs.

An empty queue is the normal state of a healthy fleet. So the lane failed whenever nothing was wrong, and the gate built to recognise that state was the thing that could not survive it.

## What was tried that did NOT work

- **Assumed the earlier 404 was still the cause.** It was not; the container had already been rebuilt, and this was a different failure wearing the same red X. Two faults in sequence look like one that was not fixed.
- **Read the run list rather than the execution payload.** The status column says `error` and nothing more; the actual message was inside n8n's flattened `execution_data` pointer array.

## The fix

Use a boolean **expression** as the left value — `={{ $json.x !== null && $json.x !== undefined }}` with operator `{type: boolean, operation: true, singleValue: true}` — which sidesteps type coercion entirely. `looseTypeValidation` is a weaker second line.

## Prevention

Stated as a design rule rather than a patch: **a null that means "normal" must not be able to fail the branch that tests for it.** Design the payload so "nothing happened" is a value the gate can read without coercion.

## How we would know it came back

A lane that errors only when there is no work. If the failures stop when the queue is busy, this is it.
