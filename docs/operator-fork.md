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
