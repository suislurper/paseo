# Operator fork

The production desktop app on this machine is built from the operator fork:

- canonical remote: `https://github.com/suislurper/paseo.git`
- canonical branch: `main`
- upstream remote: `https://github.com/getpaseo/paseo.git`

The fork carries required behavior that is not optional during upstream updates:

- multiple Claude account profiles and per-profile session history
- active provider/profile selection in the desktop app
- per-profile provider usage
- modeless cross-provider agent creation
- control-plane calls that preserve pending Claude permissions
- Paseo-owned worktree archive cleanup and live chat synchronization
- workspace-sidebar landed-to-default status, refreshed against the remote default branch
- fail-closed archive checks that distinguish landed work from unpushed or unknown work
- optional fail-closed free-space admission for **new** Paseo worktree creation only

## Worktree free-space admission

New worktree creation can require a minimum free-space floor so the host does not fill a
disk by admitting another worktree. This is operator-config only; there is no hardcoded
product default.

Config (`$PASEO_HOME/config.json`):

```json
{
  "worktrees": {
    "root": "/mnt/data/paseo-runtime/worktrees",
    "minimumFreeBytes": 68719476736
  }
}
```

- `minimumFreeBytes` is an optional nonnegative safe integer (bytes). Unset keeps upstream
  behavior (no free-space check).
- The guard runs only at the shared `createWorktreeCore` boundary used by explicit new
  worktree creation (`create_paseo_worktree`, create-agent-with-worktree, MCP
  `create_worktree`, schedules that create worktrees). It does **not** gate restore,
  recovery, archive, stop/close, or reuse of an already-registered worktree.
- Before any directory/Git/registry mutation for a **new** create, the daemon `statfs`s the
  filesystem that will hold the resolved Paseo worktrees root. If that root does not exist
  yet, it walks only to the nearest existing ancestor (nothing is created for the probe).
- Available bytes **≥** `minimumFreeBytes` pass (equality passes). Below the floor throws a
  typed error carrying available bytes, required bytes, and the checked path.
- If the guard is configured and the path cannot be resolved or `statfs` fails, creation
  fails closed before mutation.

Example operator floor for this machine: `68719476736` (64 GiB). Set it in config when
ready; do not bake it into product code.

## Build invariant

Run this before packaging:

```bash
npm run verify:operator-fork
```

`npm run build:desktop` invokes the same check automatically and refuses to package a branch
that is missing the reviewed fork history.

Production packaging additionally requires the checkout to be on local `main`, to have a remote
pointing at `suislurper/paseo`, and for `HEAD` to equal that remote's fetched `main` ref. The
explicit `--head <sha>` form checks ancestry only and is reserved for review/testing; it does not
authorize packaging.

Do not build from upstream `main`, an arbitrary feature branch, or a checkout selected only by
directory name. Verify Git history and canonical remote identity. If an upstream change supersedes
a guarded fork commit, port and review the replacement first, then update the guard in the same
change.

The guard is the executable operator-feature manifest. When a required feature is developed on a
side branch, add its reviewed integration commit to the guard before packaging. A green build from
an older, incomplete manifest is not sufficient.

## The AppImage has two halves

This is the single most important thing to know before touching a desktop build.

| resource             | built from                                             | carries                                             |
| -------------------- | ------------------------------------------------------ | --------------------------------------------------- |
| `resources/app.asar` | `packages/server/dist` (+ `cli`, `client`, `protocol`) | daemon, providers, **model manifests**              |
| `resources/app-dist` | `packages/app/dist` — an **Expo web export**           | the entire React UI: sidebar, workspaces list, chat |

`npm run build --workspace=@getpaseo/desktop` rebuilds only `app.asar`. It copies
`packages/app/dist` verbatim, whatever state it is in, and **never regenerates it**. Only the root
`npm run build:desktop` runs `expo export`, and it is the only supported packaging entry point.

Half-builds do not fail — they ship. On 2026-07-27 an agent added Opus 5 to the model manifest and
packaged via the workspace script. Opus 5 appeared in the picker (server half, freshly built) while
the landed/unpushed workspace git labels silently disappeared (UI half, a 15-day-old export). No
command exited non-zero.

Two guards now make that unpackageable, and both hang off the desktop package's own build so the
shortcut path cannot skip them:

- `npm run verify:desktop-bundle` (`prebuild`) — refuses to package when `packages/app/dist` is
  missing, is missing a guarded feature marker, or is older than `packages/app/src`. It also
  refuses when `packages/app`, `packages/server` or `packages/protocol` have uncommitted changes,
  so an artifact always corresponds to a reviewed commit.
- `npm run verify:desktop-packaged` (`postbuild`) — re-checks the markers in the artifact
  electron-builder actually produced, on every platform tree it emitted.

Feature markers live in `scripts/verify-desktop-bundle.mjs` and mirror `requiredHistory` in
`scripts/verify-operator-fork-baseline.mjs`. When you guard a new feature, add its marker in the
same change.

## Desktop build runbook

```bash
cd ~/paseo-fork
git status --short                      # must be clean
git checkout main                       # packaging requires main, not a feature branch
git push fork main && git fetch fork    # HEAD must equal the fetched fork/main
npm install                             # node_modules drifts from the lockfile after upstream merges
npm run build:desktop                   # the ONLY supported packaging path
```

Then install, keeping a labelled backup:

```bash
cp ~/Applications/Paseo-*.AppImage ~/paseo-appimage-backups/Paseo.AppImage.pre-<change>
cp packages/desktop/release/Paseo-x86_64.AppImage ~/Applications/<same filename as the backup source>
```

Replace the installed file by path. The running instance keeps its own mounted inode, so the swap is
safe while Paseo is up — but the new build only takes effect after the operator restarts Paseo.

### Known failure modes

- **`PluginError: Failed to resolve plugin for module "expo-gradle-jvmargs"`** — `node_modules` is
  behind the lockfile after an upstream merge. Run `npm install` at the repo root. Do **not** work
  around it by skipping the export and building the desktop workspace directly; that is exactly how
  a stale UI ships. `npm install` may add `peer: true` churn to `package-lock.json`; revert the
  lockfile afterwards (`git checkout package-lock.json`) unless a dependency genuinely changed.
- **`expo export` wipes `packages/app/dist` before it fails**, leaving an empty directory. Re-run
  the export; never package from that state (`verify:desktop-bundle` now refuses it).
- **Guard refuses the checkout** — you are on a feature branch, or `main` is ahead of the fetched
  `fork/main`. Land and push the work to `suislurper/paseo:main` first. Bypassing the guard is never
  the answer.
- **An agent cannot restart Paseo when it is running as a Paseo agent** — the restart kills the
  session mid-task. Install the artifact and hand the restart to the operator.

## Updating canonical main

Integrate newer upstream history with the fork features preserved. Before any non-fast-forward
replacement of the fork's `main`:

1. create and push a named backup ref for the previous fork `main`;
2. inventory every local and `suislurper/paseo` side branch and account for each operator feature;
3. update the executable feature manifest in `scripts/verify-operator-fork-baseline.mjs`;
4. verify the combined branch with focused tests, typecheck, lint, and exact-SHA review;
5. use `--force-with-lease` against the exact previously observed remote SHA;
6. verify the remote `main` SHA after the push;
7. remove integrated temporary feature refs so they cannot be mistaken for the canonical build.

Never point fork `main` at upstream history that omits the guarded operator features.

## Process census

The operator host can run a **root-owned, read-only** process census so unprivileged
cleanup probes can resolve PID reuse without scanning other users' command lines or
paths themselves.

### What it is

- Source: `scripts/operator/process-census.py`
- Unit templates: `scripts/operator/systemd/paseo-process-census.service` and
  `paseo-process-census.timer` (timer cadence: every five minutes)
- Installed executable path: `/usr/local/libexec/paseo-process-census`
- Fixed roots (production unit): `/home/user/.paseo/worktrees` and
  `/mnt/data/paseo-runtime`
- Output: `/run/paseo/process-census.json` (mode `0644`; parent `/run/paseo` mode
  `0755`)

The helper walks `/proc` **without changing processes**. The snapshot includes
`schema_version`, `boot_id`, `captured_at`, `roots`, `complete`, `errors`, and one
record per current non-kernel process. Successful records carry `pid`, Linux
`start_time_ticks`, `uid`, `name`, `scope_complete`, and `references`.

### Redaction and path scope

Each reference is only `{ "kind", "path" }` for `cwd`, `exe`, `interpreter_script`,
or `open_fd` paths that fall under a configured root. Processes with no in-root paths
get an empty `references` array — never their raw off-root paths.

The tool **must not** emit argv, command lines, environment, file contents, secrets,
or paths outside the configured roots. Cmdline is parsed in memory only to recognize
an actual interpreter script path under a root (for example `python3 job.py`);
arbitrary arguments are discarded.

Process exit races during the scan are skipped and do **not** mark the snapshot
incomplete. A permission or read error on a **still-existing** non-kernel process
sets `complete=false` and records only `pid` / `start_time_ticks` plus an error
class — no sensitive detail.

### Install and security contract

1. Install a copy of `process-census.py` to `/usr/local/libexec/paseo-process-census`.
2. The installed file **must** be **root-owned** and **not group- or world-writable**
   (typical mode `0755` or `0555`, owner `root:root`).
3. **Forbidden:** running a user-writable checkout script (or any path writable by a
   non-root user) as root via the unit or `sudo`. That would turn a cleanup helper into
   a root RCE footgun. Always execute the installed libexec path after verifying
   ownership and mode.
4. Install the systemd unit/timer from `scripts/operator/systemd/`, then enable the
   **timer** only (not ad-hoc `start` from agent sessions unless the operator asked).
5. The unit runs as root with fixed roots and output path above. Hardening that does
   not prevent full `/proc` reads or writing `/run/paseo` is allowed; do not enable
   `ProcSubset=pid`, `PrivateUsers=yes`, or other settings that hide foreign processes.

Agents in this repo must not install, start, or restart these units, and must not
mutate the live `/home/user/.paseo` probe state, unless the operator explicitly
requests that packet.

### Consumer merge rule (closes the timer race)

Snapshot freshness alone is **not** proof that a PID still refers to the same
process. The unprivileged cleanup probe must:

1. Perform its **own current** scan of the PIDs it cares about.
2. Open `/run/paseo/process-census.json` only as a **hint**.
3. Use a snapshot record for a PID **only when all** of the following hold:
   - snapshot `boot_id` matches the current kernel boot id;
   - snapshot age is **≤ 10 minutes** (`captured_at` vs now);
   - the live process’s `pid` **and** `start_time_ticks` **both** match the snapshot
     record.
4. If a PID is new or reused, or any required field is unreadable on the live process,
   treat the identity as **ambiguous and blocking** — do not delete or kill based on
   the snapshot alone.

Matching `pid + start_time_ticks` under the same `boot_id` is what closes the race
between the five-minute timer and a PID that exited and was reused.
