---
name: paseo-loop
description: Run an agent loop until an exit condition is met. Use when the user says "loop", "babysit", "keep trying until", "check every X", "watch", or wants iterative autonomous execution.
user-invocable: true
---

# Paseo Loop Skill

A loop is a worker/verifier cycle: launch a worker → check verification → repeat until done or limits hit. Use for "keep trying", "babysit", or "watch this until X."

**User's arguments:** $ARGUMENTS

## Prerequisites

Read the **paseo** skill. Before choosing worker or verifier providers, read `~/.paseo/orchestration-preferences.json` unless the user explicitly named providers in this request. Do not start the loop until you have read it.

Loops are a CLI primitive: `paseo loop run`. Manage with `paseo loop ls`, `paseo loop inspect <id>`, `paseo loop logs <id>`, `paseo loop stop <id>`. Do not use a loop as a substitute for one-off exploration; that belongs to the **paseo** skill **Research launch contract**.

## Your job

1. Understand the user's intent from `$ARGUMENTS` and the conversation.
2. **Worker prompt** — self-contained, concrete about what to do this iteration, explicit about what counts as progress.
3. **Verification** — pick the right shape:
   - Shell check (`--verify-check`) for objective criteria a command can answer (`gh pr checks --fail-fast`, `npm test`).
   - Verifier prompt (`--verify`) for judgment ("Return done=true only if all tests pass and the changed files are coherent. Cite the command and the outcome.").
   - Both, when shell rules out the obvious failures and the verifier judges the rest.
4. **Providers** — `--provider` for the worker, `--verify-provider` for the verifier. From preferences unless the user named them. For implementation loops, pair worker and verifier on different providers — each catches the other's blind spots.
5. **Sleep** — `--sleep` only when polling something external. Otherwise let it run as fast as the loop completes.
6. **Stops** — set a sensible `--max-iterations` and/or `--max-time`. Open-ended loops are how runaways happen.
7. **Archive** — `--archive` keeps agents after each iteration for inspection.
8. Launch with `paseo loop run`.

## Helper lanes vs implementation workers

If an iteration is investigation-only (trace a failure, inspect logs, gather evidence), launch that helper with the research launch contract: same workspace, explicit permission mode, no Plan, archive after consuming the result. Do not create a worktree, schedule, or nested loop for that lookup.

Implementation workers follow the repository's ownership rules (isolated writer checkout, owned paths, no shared-service mutation). They are not research children.

## Common shapes

**Babysit a PR** — worker checks PR state and fixes issues; shell check is `gh pr checks <n> --fail-fast`; sleep 2m; max-time 1h.

**Drive tests to green** — worker investigates failures and fixes code; shell check is the test command; verifier confirms all tests pass; max-iterations 10.

**Cross-provider implementation** — worker on `impl` provider, verifier on a different provider; verifier checks changed files, runs typecheck and tests; max-iterations and max-time both bounded; archive on so iterations can be inspected.

## Prompt rules

**Worker** — self-contained, concrete (commands, files, branches, tests, PRs, systems), explicit about what counts as progress this iteration.

**Verifier** — checks facts, doesn't suggest fixes, cites commands/outputs/file evidence, specific about what "done" means.
