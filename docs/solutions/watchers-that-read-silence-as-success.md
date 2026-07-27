---
id: SOL-20260727-watcher-silent-success
title: Three wait-loops reported success because their check printed nothing
status: solved
last_verified: 2026-07-27
tags: [n8n, operations]
signature: none
---

# Three wait-loops reported success because their check printed nothing

## What happened

An `until` loop was written to wait for a scheduled lane's first execution. It announced success immediately, before the lane had run at all. A second one timed out silently after thirteen minutes without ever saying what it had seen.

## Why it happened

The loop's condition compared the inner command's output against a count. When that command **errored** it printed nothing, and `"" != "0"` is true — so an empty result satisfied the success condition.

The inner command was failing for an unrelated reason (a Windows path mangled through nested shell quoting), and every failure looked exactly like the thing being waited for.

## What was tried that did NOT work

- **Inline one-liners, twice.** Both failed the same way. The quoting that broke them is also what made the failure invisible.
- **Trusting the exit code.** The loop exited 0 in one case and 1 in another for reasons unrelated to whether the event had occurred.

## The fix

A small script file that prints exactly one of `OK` / `WAITING` / `ERROR` and **never nothing**, with the loop breaking only on `OK`.

## Prevention

The rule already exists in `diagnosability.md` — *no silent empty results* — and this repo's `no-log` diagnosis tier is built on it. The mechanism is: **a watcher must distinguish "not yet" from "could not check", and print both.** Any watcher whose failure mode is silence will eventually report success.

## How we would know it came back

Ask of any wait-loop: *if the inner check errored right now, what would this print?* If the answer is "nothing", it is already broken.
