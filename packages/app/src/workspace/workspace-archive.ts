import { WorkspaceArchiveNotDispatchedError } from "@getpaseo/client/internal/daemon-client";
import type { WorkspaceRecoveryState } from "@getpaseo/protocol/messages";
import {
  clearWorkspaceArchivePending,
  markWorkspaceArchivePending,
  markWorkspaceArchiveUncertain,
} from "@/contexts/session-workspace-upserts";
import { useSessionStore, type WorkspaceDescriptor } from "@/stores/session-store";
import { resolveWorkspaceMapKeyByIdentity } from "@/utils/workspace-identity";
import { i18n } from "@/i18n/i18next";

export interface WorkspaceArchiveTarget {
  serverId: string;
  workspaceId: string;
}

export interface WorkspaceArchiveCleanup {
  status: "removed" | "retained" | "failed";
  reason?: string;
}

interface WorkspaceArchiveClient {
  inspectWorkspaceRecovery?: (workspaceId: string) => Promise<WorkspaceRecoveryState>;
  archiveWorkspace: (workspaceId: string) => Promise<{
    error: string | null;
    archivedAt?: string | null;
    cleanup?: WorkspaceArchiveCleanup;
  }>;
}

interface OptimisticWorkspaceArchiveSnapshot {
  workspace: WorkspaceDescriptor | null;
}

export interface WorkspaceArchiveFailure {
  serverId: string;
  workspaceId: string;
  error: unknown;
}

function isWorkspaceArchiveFailure(error: unknown): error is WorkspaceArchiveFailure {
  return (
    typeof error === "object" &&
    error !== null &&
    "serverId" in error &&
    typeof error.serverId === "string" &&
    "workspaceId" in error &&
    typeof error.workspaceId === "string" &&
    "error" in error
  );
}

function hideWorkspaceOptimistically(
  workspace: WorkspaceArchiveTarget,
): OptimisticWorkspaceArchiveSnapshot {
  const workspaces = useSessionStore.getState().sessions[workspace.serverId]?.workspaces;
  const workspaceKey = resolveWorkspaceMapKeyByIdentity({
    workspaces,
    workspaceId: workspace.workspaceId,
  });
  const snapshot = workspaceKey ? (workspaces?.get(workspaceKey) ?? null) : null;
  markWorkspaceArchivePending({
    serverId: workspace.serverId,
    workspaceId: workspace.workspaceId,
  });
  useSessionStore.getState().removeWorkspace(workspace.serverId, workspace.workspaceId);
  return { workspace: snapshot };
}

function restoreOptimisticallyHiddenWorkspace(input: {
  serverId: string;
  workspaceId: string;
  snapshot: OptimisticWorkspaceArchiveSnapshot;
}): void {
  clearWorkspaceArchivePending({
    serverId: input.serverId,
    workspaceId: input.workspaceId,
  });
  if (input.snapshot.workspace) {
    useSessionStore.getState().mergeWorkspaces(input.serverId, [input.snapshot.workspace]);
  }
}

class WorkspaceArchiveRejectedError extends Error {}

async function archiveWorkspaceOrThrow(input: {
  client: WorkspaceArchiveClient;
  workspaceId: string;
}): Promise<WorkspaceArchiveCleanup | undefined> {
  const payload = await input.client.archiveWorkspace(input.workspaceId);
  if (payload.error && !payload.archivedAt) throw new WorkspaceArchiveRejectedError(payload.error);
  // Record archival is durable even when a later cleanup or notification fails.
  return (
    payload.cleanup ??
    (payload.error && payload.archivedAt ? { status: "failed", reason: payload.error } : undefined)
  );
}

export async function archiveWorkspaceOptimistically(input: {
  client: WorkspaceArchiveClient;
  workspace: WorkspaceArchiveTarget;
}): Promise<WorkspaceArchiveCleanup | undefined> {
  const snapshot = hideWorkspaceOptimistically(input.workspace);

  try {
    return await archiveWorkspaceOrThrow({
      client: input.client,
      workspaceId: input.workspace.workspaceId,
    });
  } catch (error) {
    if (
      !(
        error instanceof WorkspaceArchiveRejectedError ||
        error instanceof WorkspaceArchiveNotDispatchedError
      )
    ) {
      markWorkspaceArchiveUncertain(input.workspace);
      const state = await input.client
        .inspectWorkspaceRecovery?.(input.workspace.workspaceId)
        .catch(() => null);
      if (state?.workspaceId === input.workspace.workspaceId) {
        if (
          state.kind === "recoverable" ||
          (state.kind === "unavailable" &&
            [
              "project_not_found",
              "project_directory_missing",
              "workspace_directory_missing",
              "worktree_branch_missing",
            ].includes(state.reason))
        ) {
          return { status: "failed", reason: i18n.t("sidebar.workspace.toasts.cleanupUnknown") };
        }
        if (state.kind === "unavailable" && state.reason === "workspace_not_archived") {
          restoreOptimisticallyHiddenWorkspace({ ...input.workspace, snapshot });
          throw error;
        }
      }
      // No proof of rejection: keep hidden until a fresh fetch or removal delta
      // resolves the record. An old snapshot is not rollback authority.
      throw new Error(i18n.t("sidebar.workspace.toasts.archiveUnconfirmed"), { cause: error });
    }
    restoreOptimisticallyHiddenWorkspace({ ...input.workspace, snapshot });
    throw error;
  }
}

export async function archiveWorkspacesOptimistically(input: {
  getClient: (serverId: string) => WorkspaceArchiveClient | null;
  workspaces: WorkspaceArchiveTarget[];
}): Promise<WorkspaceArchiveFailure[]> {
  const results = await Promise.allSettled(
    input.workspaces.map(async (workspace) => {
      const client = input.getClient(workspace.serverId);
      if (!client) {
        throw {
          serverId: workspace.serverId,
          workspaceId: workspace.workspaceId,
          error: new Error(i18n.t("sidebar.workspace.toasts.hostDisconnected")),
        } satisfies WorkspaceArchiveFailure;
      }

      try {
        await archiveWorkspaceOptimistically({
          client,
          workspace,
        });
      } catch (error) {
        throw {
          serverId: workspace.serverId,
          workspaceId: workspace.workspaceId,
          error,
        } satisfies WorkspaceArchiveFailure;
      }
    }),
  );

  return results.flatMap((result) =>
    result.status === "rejected" && isWorkspaceArchiveFailure(result.reason) ? [result.reason] : [],
  );
}
