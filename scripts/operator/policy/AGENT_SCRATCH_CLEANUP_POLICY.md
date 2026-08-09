# Paseo managed agent-scratch cleanup policy

Last reviewed: 2026-08-09 (Asia/Manila).

This policy owns only managed scratch under
`/mnt/data/paseo-runtime/scratch/<agent-id>/`. It never removes artifacts,
quarantine, generic `/tmp`, worktrees, or any unmanaged path. Unknown state is
a hard stop.

Repo template: `scripts/operator/policy/AGENT_SCRATCH_CLEANUP_POLICY.md`. Live
host install (when present): `/home/user/.paseo/AGENT_SCRATCH_CLEANUP_POLICY.md`.

## Durable ownership model

- Paseo creates each agent's scratch and artifact directories with mode `0700`.
- A matching daemon manifest identifies the agent and generation.
- Archive, close, tab close, age, and operator inference are **never** release.
- Cleanup requires the daemon's separate `release_agent_scratch` receipt and a
  24-hour grace period.
- `/mnt/data/paseo-runtime/artifacts/` and `quarantine/` are permanent /
  report-only for this lane: they are retained until an operator adopts a
  separate reviewed retention decision, and they survive scratch cleanup.

## Explicit release (receipt only)

Release is **not** cleanup and is not implied by lifecycle transitions:

- Archive, close, tab close, age, and any operator inference never authorize
  release or cleanup.
- The only release signal is the existing `release_agent_scratch` receipt
  (exact `agentId` + `generation`). No CLI/RPC/daemon/auto-release path is
  added by this policy or custodian wake.
- **Owner self-release:** after confirming the exact generation is rebuildable
  and no longer needed, call `release_agent_scratch` for that exact pair,
  preferably before close/archive. Hand the receipt (agent id, generation,
  `releasedAt`) into finish notes.
- **Foreign release:** an operator may release a foreign archived/closed owner
  only with explicit authority, after exact directory/generation review and the
  same rebuildability / no-longer-needed confirmation.
- Generation mismatch fails closed. Never blanket-release old agents.
- Release is receipt-only: it marks the generation `released` and begins the
  existing 24-hour grace. It does not delete files, archive the agent, or
  close a runtime.
- After grace, all existing cleanup gates still apply: descendants, schedules,
  terminals, permissions, processes, detached owners, locks, and inventory
  gates. Artifacts and quarantine remain permanent / report-only and survive
  scratch cleanup.

## Required wake procedure

1. Run the canonical read-only probe once:

   ```bash
   python3 /home/user/.paseo/bin/agent-scratch-cleanup.py \
     --config /home/user/.paseo/config.json probe
   ```

2. Require a complete, same-boot, post-start root process snapshot and complete
   Paseo agent, descendant, schedule, terminal, permission, lock, and bounded
   size inventories. Any malformed, missing, stale, page-capped, changing, or
   ambiguous state blocks every affected candidate as reported by the probe.
3. The probe reports **every** UUID candidate with review metadata even under
   global blocks: `agent_id`, `generation`, `manifest_lifecycle`, normalized
   `owner_state`, `size_bytes`, and `reasons`. Each candidate size walk has its
   own 60-second bound; unknown size is unknown (`null`), never zero.
4. Canonical probe top-level `size_summary` is the only managed-scratch size
   aggregate. It deterministically sums already-measured per-candidate
   `size_bytes` (`candidate_count`, `known_count`, `unknown_count`, `bytes`).
   Never run a second whole-root `du` or extra size walk. If any candidate size
   is unknown, aggregate `bytes` is `null` / unknown; report exact counts and
   blockers.
5. **Operator review list:** from probe output, produce exact rows with agent
   ID, generation, lifecycle, owner state, size, and blockers. Missing
   manifest and ambiguous owner remain protected / blocked as the probe
   reports; never invent eligibility.
6. Classify every managed scratch directory as `protected`, `blocked`, or
   `eligible`. Eligibility requires all of the following:
   - matching manifest, agent ID, generation, and release receipt;
   - release age of at least 24 hours;
   - the agent is archived or closed;
   - no unarchived descendant, active schedule, terminal, permission, process,
     detached owner, or lock can still use the directory;
   - two complete matching inventories and a stable candidate token.
7. For an eligible candidate, run one exact archive call using only values from
   the fresh probe:

   ```bash
   python3 /home/user/.paseo/bin/agent-scratch-cleanup.py \
     --config /home/user/.paseo/config.json archive \
     --agent-id <exact-uuid> \
     --generation <exact-generation-uuid> \
     --candidate-token <exact-token>
   ```

   The archive command takes no raw path and repeats every safety gate. Never
   synthesize IDs, generations, tokens, or paths.

8. Work sequentially. Across the worktree and managed-scratch lanes combined,
   perform at most eight cleanup actions in one wake. Stop on the first error,
   state change, incomplete census, or when root free space reaches 128 GiB.
9. Re-run both read-only probes after action. Report exact owners, paths,
   generations, measured bytes, free bytes before/after, probe `size_summary`,
   preserved artifacts, retained quarantine, blockers, and the next scheduled
   action.

## Legacy `/tmp`

The recurring wake never deletes or quarantines generic `/tmp`. Legacy recovery
uses `/home/user/.paseo/bin/legacy-tmp-quarantine.py` only during an explicitly
attended operator run, one recognized exact top-level source at a time, with a
durable verified copy under
`/mnt/data/paseo-runtime/quarantine/legacy-tmp/` before source removal. See
`docs/operator-fork.md` for the closed `pre-runtime-scratch-layout` profile and
`finalize-existing` workflow.

The custodian may not weaken gates, break locks, kill processes, alter agents or
schedules to manufacture eligibility, delete artifacts/quarantine, or restart
Paseo. The SHAB controller's private checkout lifecycle is outside this
custodian lane.
