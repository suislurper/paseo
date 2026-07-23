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

## Build invariant

Run this before packaging:

```bash
npm run verify:operator-fork
```

`npm run build:desktop` invokes the same check automatically and refuses to package a branch
that is missing the reviewed fork history.

Do not build from upstream `main`, an arbitrary feature branch, or a checkout selected only by
directory name. Verify Git history. If an upstream change supersedes a guarded fork commit,
port and review the replacement first, then update the guard in the same change.

## Updating canonical main

Integrate newer upstream history with the fork features preserved. Before any non-fast-forward
replacement of the fork's `main`:

1. create and push a named backup ref for the previous fork `main`;
2. verify the combined branch with focused tests, typecheck, lint, and exact-SHA review;
3. use `--force-with-lease` against the exact previously observed remote SHA;
4. verify the remote `main` SHA after the push.

Never point fork `main` at upstream history that omits the guarded operator features.
