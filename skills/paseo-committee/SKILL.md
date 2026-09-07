---
name: paseo-committee
description: Form a committee of two high-reasoning agents to step back, do root cause analysis, and produce a plan. Use when stuck, looping, tunnel-visioning, or facing a hard planning problem.
user-invocable: true
---

# Committee Skill

Two agents from contrasting providers, fresh context, producing an advisory plan in parallel.

The purpose is to step back, not double down. The committee may propose a completely different approach. It does not implement, and it does not substitute for or reopen a repository's governed final review.

**User's additional context:** $ARGUMENTS

## Prerequisites

Read the **paseo** skill. Before choosing committee members, read `~/.paseo/orchestration-preferences.json` unless the user explicitly named providers in this request. Launch each member with that skill's **Research launch contract**. Do not invent a permission gate before spawning.

Contrast is the point of a committee, so pick across providers deliberately using the configured preferences rather than hardcoded defaults.

## Composition

Two members with different reasoning styles, selected from orchestration preferences:

- one planning/research-strength provider
- one contrasting high-reasoning provider

Override only when the user explicitly asks for different members.

## Hard rules

- **No edits.** Every prompt to a committee member ends with the no-edits suffix:

  ```
  This is analysis only. Do NOT edit, create, or delete any files. Do NOT write code.
  ```

- **Trust the wait.** Do not poll, send hurry-ups, or interrupt. Long waits mean it found something worth thinking about.
- **Advisory only.** Synthesize the plan for the orchestrator. Do not implement from this skill. Do not treat committee output as governed final review.

## Deliberate

Write a problem-level prompt:

- High-level goal and acceptance criteria
- Constraints
- Symptoms (if a bug)
- What you tried and why it failed
- Explicit: "do root cause analysis"
- Explicit: "state assumptions, ask why three levels deep, check whether you're patching a symptom or removing the problem"

Create both agents in parallel via Paseo with `[Committee] <task>` titles and the same prompt. Default: temporary, same workspace. Use the research launch contract for relationship, workspace, permission mode, Codex `plan_mode`, thinking, and `lifecycle` / `cleanup` labels. Wait for both — not just whichever finishes first.

Read both responses. Challenge them — do not accept at face value:

- "Why does <underlying thing> happen? Symptom or cause?"
- Verify any assumption the plan makes about the code.
- "What did you consider and reject?"

Send follow-ups until the plan addresses root cause.

Synthesize:

- Convergence → unified plan.
- Significant divergence → involve the user.

Confirm the merged plan with both members. Then stop. Finish follow-ups and archive both members unless the user asks to keep an advisor around.
