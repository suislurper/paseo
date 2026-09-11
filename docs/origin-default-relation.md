# Origin default relation

`originDefaultRelation` describes how a checkout's `HEAD` relates to the resolved
**origin default tip**. Resolution requires authoritative
`refs/remotes/origin/HEAD` evidence: a valid symbolic-ref whose target is under
`refs/remotes/origin/*` and whose target ref exists. Missing, malformed,
wrong-namespace, or dangling targets yield `unverifiable` with null
counts/ref as appropriate — there is **no** fall back to local `main`/`master`
for this safety field (local heuristics remain only for unrelated non-safety
base operations such as `baseRef`). It is additive protocol data about merge
history. It does not establish whether an unmerged branch is preserved remotely.

## Ancestry vs patch equivalence

| State                           | Meaning                                                                          | Safety for auto-archive          |
| ------------------------------- | -------------------------------------------------------------------------------- | -------------------------------- |
| `exact`                         | `HEAD` OID equals the origin default tip                                         | Safe evidence (with other gates) |
| `included`                      | `HEAD` is an ancestor of origin default                                          | Safe evidence (with other gates) |
| `ahead`                         | Origin default is an ancestor of `HEAD`; unique commits remain                   | Not safe                         |
| `patch_equivalent_not_included` | Same tree / no unique patches, but **not** ancestral inclusion (e.g. squash tip) | Not safe — still protected       |
| `diverged_with_unique_commits`  | Diverged with unique patch content                                               | Not safe                         |
| `unverifiable`                  | Missing remote default, detached ambiguity, or proof budget exceeded             | Not safe                         |

**Ancestral inclusion** (`exact` / `included`) means every commit on `HEAD` is
already reachable from origin default. The work has landed in the history sense.

**Patch equivalence** is weaker. A squash merge can leave a feature tip with a
different commit graph that happens to produce the same tree (or cherry-equivalent
patches). That is **not** ancestral inclusion. The branch is still treated as
protected: the sidebar says "Changes equivalent to merged work", and auto-archive
will not remove it on that evidence alone.

Legacy `aheadOfOrigin` / `behindOfOrigin` continue to mean "ahead of the branch's
configured upstream," not "ahead of origin default." Do not conflate the two.

## Remote preservation is separate

`remotePreservation` describes whether fetched remote-tracking history contains the
exact local tip. Its `state` is `preserved`, `unpreserved`, or `unknown`; `ref`
identifies a containing remote ref, and `localCommitCount` is unknown when the
count could not be established. Being ahead of the default branch never means
"unpushed" by itself.

The sidebar, archive confirmation and project archive share these meanings:

- Merged into the resolved default branch.
- Preserved remotely; not merged (or merge status unknown).
- Local commits not preserved remotely.
- Changes equivalent to merged work.
- Status unknown.

These are display observations from fetched refs, not deletion authority. Cleanup
freshly checks remote advertisements for a ref containing the exact tip, with a
bounded lookup; an offline remote or stale tracking ref retains the checkout.

## Explicit archive authority

### Manual archive

Archival keeps conversations. Its structured `cleanup` result separately reports
`removed`, `retained` with a reason, or `failed`. External workspaces can archive
their records; their files remain under the owning repository's closeout procedure.
`archive_only` never runs worktree teardown or deletes checkout files. New clients
require the host's `workspaceArchiveModes` capability for record-only archival and
`workspaceSafeCleanup` for reclamation; old hosts cannot silently ignore a mode.

For cleanup, the server requires an isolated Paseo-owned checkout, no active
workspace/agent/process reference, and the released implementation-writer lock.
It holds that same lock through inspection, teardown, fresh preservation checks
and Git removal. Dirty/hidden-index changes, unknown ignored files, protected
evidence, model artifacts, mounts, and uncertain ownership retain files. Required
agent/terminal or script teardown failures also retain them. There is no forced
Git removal or recursive deletion fallback in archival.

Before removal, the exact commit is saved on every archived workspace record that
uses the checkout. Git removes the worktree without force, then attempts normal
branch deletion only while the branch still points to the saved tip. Restore uses
the saved commit on a fresh branch, so later reuse of a branch name cannot restore
different code. Historical records without a saved commit keep their branch-based
recovery. Failed cleanup and notification do not resurrect an archived record;
repeating archival can finish eligible residual cleanup.

### Auto-archive after merge

Auto-archive is **fail closed**. All of the following are required:

1. Explicit setting `autoArchiveAfterMerge === true`
2. Merged pull request observed for that checkout
3. **Forced fresh** git snapshot (`force: true`, `includeForge: false`, reason
   `auto-archive-on-merge`) — never gate on a cached snapshot
4. Clean working tree — `isDirty === false` (null/undefined is unknown and fails)
5. Named branch (not detached)
6. Verifiable `originDefaultRelation` with state `exact` or `included`
7. Relation `ahead === 0` (null/undefined/nonzero fail)
8. Relation `uniquePatchCount === 0` (null/undefined/nonzero fail)
9. Branch upstream `aheadOfOrigin === 0` (null/undefined/nonzero fail)
10. Paseo-owned worktree (founder / non-Paseo checkouts are never auto-archived)
11. Successful workspace resolution and archive path (errors skip, never force)

Unknown fields never pass: missing relation (old daemon shape), `null`/`undefined`
counts, non-zero ahead, non-safe relation states (`ahead`,
`patch_equivalent_not_included`, `diverged_with_unique_commits`, `unverifiable`),
dirty or unknown dirty trees, or archive failures **skip**. Inclusion is evidence
used **only after** the explicit auto-archive setting; it never initiates archive
by itself.

## Cross-worktree refresh

Worktrees that share a git common directory share remote-default refs
(`refs/remotes/origin/*`, `packed-refs`, local default under `refs/heads`).
`WorkspaceGitService` watches those paths once per common dir and schedules a
**forced** debounced refresh (`force: true`, `includeForge: false`, reason
`common-dir-refs`) for every registered workspace so a main-sync or Queue publish
in one checkout updates `originDefaultRelation` (and `checkout_status_update` /
`gitRuntime`) for siblings promptly. Force bypasses the 2s non-forced internal
throttle while debounce still coalesces watcher storms; no forge/network work.
