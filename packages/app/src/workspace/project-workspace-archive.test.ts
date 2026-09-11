import { describe, expect, it, vi } from "vitest";
import { selectProjectWorkspacesToArchive } from "@/workspace/project-workspace-archive";

describe("selectProjectWorkspacesToArchive", () => {
  it("skips archiving a dirty and unpreserved worktree when the risky archive confirmation is canceled", async () => {
    const confirmWorktreeArchive = vi.fn(async () => false);

    const targets = await selectProjectWorkspacesToArchive(
      [
        {
          serverId: "server-1",
          workspaceId: "workspace-worktree",
          workspaceKind: "worktree",
          name: "feature/risky",
          archiveHasUncommittedChanges: true,
          archiveOriginDefaultRelation: null,
          archiveRemotePreservation: { state: "unpreserved", ref: null, localCommitCount: 2 },
          diffStat: { additions: 5, deletions: 1 },
        },
        {
          serverId: "server-1",
          workspaceId: "workspace-checkout",
          workspaceKind: "local_checkout",
          name: "main",
          archiveHasUncommittedChanges: null,
          archiveOriginDefaultRelation: null,
          archiveRemotePreservation: null,
          diffStat: null,
        },
      ],
      confirmWorktreeArchive,
    );

    expect(confirmWorktreeArchive).toHaveBeenCalledOnce();
    expect(confirmWorktreeArchive).toHaveBeenCalledWith({
      workspaceName: "feature/risky",
      isDirty: true,
      remotePreservation: { state: "unpreserved", ref: null, localCommitCount: 2 },
      originDefaultRelation: null,
      diffStat: { additions: 5, deletions: 1 },
    });
    expect(targets).toEqual([
      {
        serverId: "server-1",
        workspaceId: "workspace-checkout",
      },
    ]);
  });

  it("includes a dirty and unpreserved worktree when the risky archive confirmation is accepted", async () => {
    const confirmWorktreeArchive = vi.fn(async () => true);

    const targets = await selectProjectWorkspacesToArchive(
      [
        {
          serverId: "server-1",
          workspaceId: "workspace-worktree",
          workspaceKind: "worktree",
          name: "feature/risky",
          archiveHasUncommittedChanges: true,
          archiveOriginDefaultRelation: null,
          archiveRemotePreservation: { state: "unpreserved", ref: null, localCommitCount: 2 },
          diffStat: { additions: 5, deletions: 1 },
        },
        {
          serverId: "server-1",
          workspaceId: "workspace-checkout",
          workspaceKind: "local_checkout",
          name: "main",
          archiveHasUncommittedChanges: null,
          archiveOriginDefaultRelation: null,
          archiveRemotePreservation: null,
          diffStat: null,
        },
      ],
      confirmWorktreeArchive,
    );

    expect(confirmWorktreeArchive).toHaveBeenCalledOnce();
    expect(confirmWorktreeArchive).toHaveBeenCalledWith({
      workspaceName: "feature/risky",
      isDirty: true,
      remotePreservation: { state: "unpreserved", ref: null, localCommitCount: 2 },
      originDefaultRelation: null,
      diffStat: { additions: 5, deletions: 1 },
    });
    expect(targets).toEqual([
      {
        serverId: "server-1",
        workspaceId: "workspace-worktree",
      },
      {
        serverId: "server-1",
        workspaceId: "workspace-checkout",
      },
    ]);
  });
});
