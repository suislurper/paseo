import { runGitCommand } from "./run-git-command.js";
import { inspectDisposableCheckout } from "./worktree-cleanup-safety.js";
import {
  verifyRemotePreservation,
  withCheckoutWriteLock,
  WorktreeCleanupHold,
} from "./worktree-preservation.js";

/** Callers must persist recovery metadata before the checkout can disappear. */
export async function removeVerifiedWorktree(options: {
  cwd: string;
  worktree: string;
  persistHead: (head: string) => Promise<void>;
  validateReferences: () => Promise<void>;
  teardown: () => Promise<void>;
  procRoot?: string;
  runtimeDirectory?: string;
}): Promise<{ branchRemoved: boolean }> {
  const identity = await runGitCommand(["rev-parse", "--absolute-git-dir"], {
    cwd: options.worktree,
  });
  if (identity.truncated) throw new WorktreeCleanupHold("Checkout identity could not be read.");
  return withCheckoutWriteLock(
    identity.stdout.trim(),
    async (assertHeld) => {
      await options.validateReferences();
      // Check ownership before user teardown commands can touch checkout files.
      await inspectDisposableCheckout(options.worktree, { procRoot: options.procRoot });
      await options.teardown();
      const before = await inspectDisposableCheckout(options.worktree, {
        procRoot: options.procRoot,
      });
      const preservingRef = await verifyRemotePreservation(options.worktree, before.head);
      await options.persistHead(before.head);
      await options.validateReferences();
      await assertHeld();
      const after = await inspectDisposableCheckout(options.worktree, {
        procRoot: options.procRoot,
      });
      if (JSON.stringify(before) !== JSON.stringify(after)) {
        throw new WorktreeCleanupHold("The checkout changed during cleanup; files retained.");
      }
      await runGitCommand(["worktree", "remove", "--", options.worktree], {
        cwd: options.cwd,
        timeout: 120_000,
      });
      // Git checks the CURRENT branch tip against the verified upstream and
      // refuses if it is checked out elsewhere. No force or raw ref deletion.
      try {
        const branch = await runGitCommand(
          ["rev-parse", "--verify", "refs/heads/" + before.branch],
          { cwd: options.cwd },
        );
        if (branch.stdout.trim() !== before.head) return { branchRemoved: false };
        const remoteNames = await runGitCommand(["remote"], { cwd: options.cwd });
        const short = preservingRef.slice("refs/remotes/".length);
        const remote = remoteNames.stdout
          .trim()
          .split("\n")
          .sort((a, b) => b.length - a.length)
          .find((name) => name && short.startsWith(name + "/"));
        if (!remote) return { branchRemoved: false };
        await assertHeld();
        await runGitCommand(
          [
            "-c",
            "branch." + before.branch + ".remote=" + remote,
            "-c",
            "branch." + before.branch + ".merge=refs/heads/" + short.slice(remote.length + 1),
            "branch",
            "-d",
            "--",
            before.branch,
          ],
          { cwd: options.cwd },
        );
        return { branchRemoved: true };
      } catch {
        // Checkout removal succeeded; retaining a branch never reverses archival.
        return { branchRemoved: false };
      }
    },
    options.runtimeDirectory,
  );
}
