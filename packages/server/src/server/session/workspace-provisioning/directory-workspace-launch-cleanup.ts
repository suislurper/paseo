import type { Logger } from "pino";
import type { PersistedWorkspaceRecord, WorkspaceRegistry } from "../../workspace-registry.js";

export type DirectoryWorkspaceLaunchReferenceInspection =
  | {
      available: true;
      hasManagedAgent: boolean;
      hasPersistedAgent: boolean;
      hasTerminal: boolean;
    }
  | {
      available: false;
    };

export interface DirectoryWorkspaceLaunch {
  readonly workspaceId: string;
  readonly workspace: PersistedWorkspaceRecord;
  retain(): void;
  commit(): void;
  cleanupUnusedOnFailure(): Promise<void>;
}

export interface OpenDirectoryWorkspaceLaunchInput {
  workspace: PersistedWorkspaceRecord;
  workspaceRegistry: WorkspaceRegistry;
  logger: Logger;
  inspectReferences?: (workspaceId: string) => Promise<DirectoryWorkspaceLaunchReferenceInspection>;
}

interface DirectoryWorkspaceLaunchClaim {
  owner: symbol;
  retained: boolean;
}

const claimsByRegistry = new WeakMap<
  WorkspaceRegistry,
  Map<string, DirectoryWorkspaceLaunchClaim>
>();

function claimsFor(
  workspaceRegistry: WorkspaceRegistry,
): Map<string, DirectoryWorkspaceLaunchClaim> {
  const existing = claimsByRegistry.get(workspaceRegistry);
  if (existing) return existing;
  const created = new Map<string, DirectoryWorkspaceLaunchClaim>();
  claimsByRegistry.set(workspaceRegistry, created);
  return created;
}

export function retainDirectoryWorkspaceLaunch(
  workspaceRegistry: WorkspaceRegistry,
  workspaceId: string,
): void {
  const claim = claimsFor(workspaceRegistry).get(workspaceId);
  if (!claim) return;
  claim.retained = true;
}

export function openDirectoryWorkspaceLaunch(
  input: OpenDirectoryWorkspaceLaunchInput,
): DirectoryWorkspaceLaunch {
  const { workspace, workspaceRegistry, logger, inspectReferences } = input;
  const workspaceId = workspace.workspaceId;
  const claims = claimsFor(workspaceRegistry);
  if (claims.has(workspaceId)) {
    throw new Error(`Directory workspace launch already owned: ${workspaceId}`);
  }
  const owner = Symbol(workspaceId);
  claims.set(workspaceId, { owner, retained: false });

  function releaseIfOwner(): void {
    const claim = claims.get(workspaceId);
    if (!claim) return;
    if (claim.owner !== owner) return;
    claims.delete(workspaceId);
  }

  function retain(): void {
    retainDirectoryWorkspaceLaunch(workspaceRegistry, workspaceId);
  }

  function commit(): void {
    releaseIfOwner();
  }

  async function inspectReferencesSafely(): Promise<"preserve" | "unreferenced"> {
    if (!inspectReferences) return "preserve";
    let inspection: DirectoryWorkspaceLaunchReferenceInspection;
    try {
      inspection = await inspectReferences(workspaceId);
    } catch {
      return "preserve";
    }
    if (inspection.available !== true) return "preserve";
    const referenced =
      inspection.hasManagedAgent || inspection.hasPersistedAgent || inspection.hasTerminal;
    if (referenced) return "preserve";
    return "unreferenced";
  }

  function shouldSkipArchive(record: PersistedWorkspaceRecord): boolean {
    const claim = claims.get(workspaceId);
    if (!claim) return true;
    if (claim.owner !== owner) return true;
    if (claim.retained) return true;
    if (record.archivedAt) return true;
    if (record.kind === "worktree") return true;
    return false;
  }

  async function cleanupUnusedOnFailure(): Promise<void> {
    try {
      const claim = claims.get(workspaceId);
      if (!claim || claim.owner !== owner) return;
      if (claim.retained) return;

      const inspection = await inspectReferencesSafely();
      const claimAfterInspect = claims.get(workspaceId);
      if (!claimAfterInspect || claimAfterInspect.owner !== owner) return;
      if (claimAfterInspect.retained) return;
      if (inspection !== "unreferenced") return;

      await workspaceRegistry.update(workspaceId, (record) => {
        if (shouldSkipArchive(record)) return record;
        const archivedAt = new Date().toISOString();
        return {
          ...record,
          archivedAt,
          updatedAt: archivedAt,
        };
      });
    } catch (error) {
      logger.error(
        { err: error, workspaceId },
        "Failed to clean up unused directory workspace after launch failure",
      );
    } finally {
      releaseIfOwner();
    }
  }

  return {
    workspaceId,
    workspace,
    retain,
    commit,
    cleanupUnusedOnFailure,
  };
}
