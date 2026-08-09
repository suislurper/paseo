# Paseo worktree cleanup policy

Last reviewed: 2026-08-09 (Asia/Manila).

This policy keeps `/home` from filling with abandoned Paseo worktrees without
trading disk pressure for lost work. Cleanup is automatic only when every
safety gate below is proven. Unknown state is a hard stop for that checkout.
This file owns only the worktree lane; managed agent scratch is governed by
`AGENT_SCRATCH_CLEANUP_POLICY.md` (live host:
`/home/user/.paseo/AGENT_SCRATCH_CLEANUP_POLICY.md`).

Repo template: `scripts/operator/policy/WORKTREE_CLEANUP_POLICY.md`. Live host
install (when present): `/home/user/.paseo/WORKTREE_CLEANUP_POLICY.md`.

## Disk thresholds

- Warning floor: 120 GiB free on the filesystem containing `/home`.
- Target after cleanup: 128 GiB free.
- Critical floor: 64 GiB free.
- Disk pressure never weakens a safety gate. If no safe candidate exists,
  report the exact blockers and wait for the operator.

When free space is below 128 GiB, evaluate eligible worktrees largest first and
archive at most eight in one wake. When free space is at or above 128 GiB,
archive only eligible worktrees older than 72 hours. Below 128 GiB, a candidate
must still be at least 12 hours old.

## Permanent gates for automatic archive

A worktree may be archived automatically only when all of these are true at
inventory time and again immediately before archive:

1. Paseo reports the exact path as a Paseo-managed worktree. Unmanaged paths,
   stale directories, ordinary clones, and the shared/main checkout are report
   only. Never bypass Paseo with `rm`, `git worktree remove`, or branch deletion.
2. No unarchived agent has a `cwd` equal to or below the worktree. Treat
   `initializing`, `running`, `idle`, `error`, and unarchived `closed` records as
   owners. Resolve list inconsistencies with exact `get_agent_status` calls; an
   unresolved owner blocks archive.
3. No active schedule targets an agent in the worktree. No terminal, pending
   permission, named checkout lock, Git worktree lock, or OS process has a cwd
   or command path inside it. Never stop or kill a process to make a candidate.
4. `git status --porcelain=v1 -uall` is empty, including submodule state. The
   branch is named, the Git identity resolves to a linked worktree, and the
   checkout is not the repository's main checkout.
5. The remote default is resolved authoritatively with read-only remote
   inspection. Its exact commit object is available locally, the worktree HEAD
   is an ancestor of that exact remote-default commit, and the worktree has
   zero commits ahead. Patch equivalence, squash equivalence, Queue metadata,
   a landed notification, or a local default branch is not enough.
6. Ignored content contains only rebuildable caches or dependency trees:
   `node_modules`, `.next`, `.turbo`, `.cache`, `.pytest_cache`, `.mypy_cache`,
   `.ruff_cache`, `.import_linter_cache`, `__pycache__`, generated package
   `dist`, and bytecode. An ignored `.env` or `.env.local` is allowed only when
   it is a symlink to, or byte-for-byte identical with, the corresponding file
   in the canonical checkout. Any other ignored path blocks archive.
7. In particular, never auto-delete ignored or untracked paths named or
   containing `data`, `runs`, `artifacts`, `results`, `receipts`, `evidence`,
   `logs`, `snapshots`, `backups`, `.done`, databases, model outputs, or source
   corpora. Their size does not matter.
8. The candidate is not covered by a manual pin below. A pin remains until the
   operator edits this policy, even if the agent later becomes idle or archived.

## Manual pins

The following agents and every checkout below their cwd are protected by the
operator's 2026-08-04 instruction:

- `97da9be5-be19-48d1-aed1-c50692eef59e`
- `5182e9a1-718a-4acc-af52-dc956a519c81`
- `bb912b97-661c-40e1-801a-d405f0829103`
- `f77f6615-d886-4e71-ba3b-93cf8f94be76`

A pin protects the agent's last exact checkout path if that path exists now or
reappears later. An already-absent pinned path is recorded as `pinned-absent`;
it is never recreated or treated as permission to clean a replacement path,
but its absence does not block classification of unrelated exact paths.

## Required wake procedure

1. Read this file, `/home/user/.agents/skills/paseo/SKILL.md`, the target
   repository's `AGENTS.md`, and its worktree procedure before action.
2. Use Paseo's `list_worktrees` operation with `cwd=/mnt/data/shab` for the
   authoritative managed-path and creation-time census. Then run the canonical
   read-only local probe exactly once for the initial local census:

   ```bash
   python3 /home/user/.paseo/bin/worktree_cleanup_probe.py
   ```

   The probe emits one JSON document and never archives or mutates anything.
   Match its `worktrees[].path` values exactly against Paseo's managed paths.
   A Paseo-managed path missing from either set makes the inventory incomplete
   and stops the wake. Probe-only paths are unmanaged and report-only. Do not
   recreate its Git-porcelain, ignored-root, process, lock, ancestry, or bounded
   size logic in ad-hoc shell or JavaScript, and do not encode inventory through
   runtime-specific helpers such as `btoa`.

3. Record free bytes, all managed worktrees, all active/unarchived agents,
   active schedules, terminals, pending permissions, processes, and exact Git
   evidence. Do not rely on a cached sidebar label.
   - Use `paseo ls --global --json` for the unarchived-agent census; do not add
     `--all`, because archived records are not owners and can exceed the API
     page cap. Inspect every manual-pin agent ID separately so an archived pin
     is still enforced. If the unarchived result reaches 200, use the Paseo
     agent-list API with a page limit of 200 or less and consume every page. A
     rejected or incomplete census blocks the wake; do not improvise a larger
     limit.
   - Parse the CLI census with the static, type-safe shape
     `.[] | select((.cwd | type) == "string") | [.id, .status, (.cwd | sub("^~"; "/home/user"))] | @tsv`.
     Do not splice shell variables into
     this `jq` program. A non-string or missing `cwd` on an unarchived record
     requires exact `paseo inspect <agent-id>` resolution; never discard it.
   - Do not recursively enumerate dependency trees. Use ordinary
     `git status --porcelain=v1 -uall` for tracked/untracked state and
     `git status --porcelain=v1 --ignored=matching -unormal` only to classify
     ignored roots. Never combine recursive ignored output with `-uall`.
   - Parse ignored paths as data. Do not interpolate a path into an `awk`,
     `grep`, or shell regular expression. Strip the literal `!! ` status prefix
     and compare normalized path components against the allowlist and protected
     names above. Any parse error makes ignored content unknown and blocks only
     that exact candidate unless the inventory itself is incomplete.
   - Process ownership comes only from the root-owned redacted snapshot at
     `/run/paseo/process-census.json`, merged with a current unprivileged
     PID/start-time census by the canonical probe. The accepted snapshot must be
     complete, same-boot, captured after the probe started, no older than 45
     seconds, and cover both the managed worktree root and the repository's
     common Git directory. Any mismatch, reuse, unreadable identity, malformed
     reference, missing root coverage, or surviving diagnostic child blocks
     every candidate. Do not substitute `ps`, `fuser`, command lines, or added
     elevation during a scheduled wake.
   - Bound each candidate size walk to 60 seconds. A timeout makes that
     candidate's size unknown and blocks archive for that wake; it is not a
     reason to retry or weaken gates. Unknown size is never reported as zero.
   - Canonical probe top-level `size_summary` is the **only** aggregate for
     managed worktrees. It deterministically sums already-measured 60-second
     per-candidate `size_bytes` values (`candidate_count`, `known_count`,
     `unknown_count`, and `bytes`). Never run a second whole-root `du` or any
     other size walk to produce a total. If any candidate size is unknown,
     aggregate `bytes` is `null` / unknown; report exact known/unknown counts
     and blockers.
4. Classify every worktree as `protected`, `blocked`, or `eligible`, with the
   exact reason. Do not append repeat reports for unchanged healthy state.
5. If action is needed, re-list Paseo worktrees, then re-run the probe for only
   that exact path:

   ```bash
   python3 /home/user/.paseo/bin/worktree_cleanup_probe.py --path <exact-path>
   ```

   Require `complete=true`, `local_gate=pass`, and the same
   `candidate_token` as the initial probe. Re-check the Paseo creation-time age
   and every external reference gate, then call Paseo's exact-path
   `archive_worktree` operation. Archive sequentially. Stop on the first
   unexpected error or state change.

6. Re-list worktrees and agents, verify every pinned path still exists, verify
   archived paths are gone, and record free bytes after cleanup.
7. Report exact paths removed, actual bytes freed from `df`, protected owners,
   blockers, probe `size_summary`, and the next scheduled action. Schedule logs
   are the durable receipt for each wake.

The custodian has no authority to edit repositories, change branches or refs,
kill processes, restart Paseo, mutate services/databases/data, delete unmanaged
directories, or weaken these gates. The SHAB controller's private checkout
lifecycle is outside this custodian lane.
