import type { WorkspaceDescriptor } from "@/stores/session-store";
import { normalizeWorkspaceOpaqueId } from "@/utils/workspace-identity";

interface PendingWorkspaceArchive {
  workspaceId: string;
  uncertain: boolean;
}

const pendingWorkspaceArchivesByServer = new Map<string, Map<string, PendingWorkspaceArchive>>();

function pendingArchiveKey(input: { serverId: string; workspaceId: string }): string {
  return `${input.serverId.trim()}::${input.workspaceId.trim()}`;
}

export function markWorkspaceArchivePending(input: {
  serverId: string;
  workspaceId: string;
}): void {
  const serverId = input.serverId.trim();
  const workspaceId = normalizeWorkspaceOpaqueId(input.workspaceId);
  if (!serverId || !workspaceId) {
    return;
  }

  const archives = pendingWorkspaceArchivesByServer.get(serverId) ?? new Map();
  archives.set(pendingArchiveKey({ serverId, workspaceId }), {
    workspaceId,
    uncertain: false,
  });
  pendingWorkspaceArchivesByServer.set(serverId, archives);
}

export function clearWorkspaceArchivePending(input: {
  serverId: string;
  workspaceId: string;
}): void {
  const serverId = input.serverId.trim();
  const workspaceId = normalizeWorkspaceOpaqueId(input.workspaceId);
  if (!serverId || !workspaceId) {
    return;
  }

  const archives = pendingWorkspaceArchivesByServer.get(serverId);
  if (!archives) {
    return;
  }
  archives.delete(pendingArchiveKey({ serverId, workspaceId }));
  if (archives.size === 0) {
    pendingWorkspaceArchivesByServer.delete(serverId);
  }
}

export function isWorkspaceArchivePending(input: {
  serverId: string;
  workspaceId?: string | null;
}): boolean {
  const serverId = input.serverId.trim();
  if (!serverId) {
    return false;
  }

  const archives = pendingWorkspaceArchivesByServer.get(serverId);
  if (!archives) {
    return false;
  }

  const workspaceId = normalizeWorkspaceOpaqueId(input.workspaceId);
  return Boolean(workspaceId && archives.has(pendingArchiveKey({ serverId, workspaceId })));
}

export function shouldSuppressWorkspaceForLocalArchive(input: {
  serverId: string;
  workspace: WorkspaceDescriptor;
}): boolean {
  return isWorkspaceArchivePending({
    serverId: input.serverId,
    workspaceId: input.workspace.id,
  });
}

export function markWorkspaceArchiveUncertain(input: {
  serverId: string;
  workspaceId: string;
}): void {
  const record = pendingWorkspaceArchivesByServer
    .get(input.serverId.trim())
    ?.get(pendingArchiveKey(input));
  if (record) record.uncertain = true;
}

/** Capture before a fresh complete directory fetch, never from an old buffered snapshot. */
export function captureUncertainWorkspaceArchives(
  serverId: string,
): readonly PendingWorkspaceArchive[] {
  return [...(pendingWorkspaceArchivesByServer.get(serverId.trim())?.values() ?? [])].filter(
    (record) => record.uncertain,
  );
}

export function clearReconciledWorkspaceArchives(
  serverId: string,
  records: readonly PendingWorkspaceArchive[],
): void {
  const current = pendingWorkspaceArchivesByServer.get(serverId.trim());
  for (const record of records) {
    const input = { serverId, workspaceId: record.workspaceId };
    if (current?.get(pendingArchiveKey(input)) === record && record.uncertain)
      clearWorkspaceArchivePending(input);
  }
}
