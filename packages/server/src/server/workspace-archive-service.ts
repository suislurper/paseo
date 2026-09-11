import { dirname, resolve } from "node:path";

import type { Logger } from "pino";

import type { AgentManager } from "./agent/agent-manager.js";
import type { AgentStorage } from "./agent/agent-storage.js";
import type { WorkspaceGitService } from "./workspace-git-service.js";
import type { ForgeService } from "../services/forge-service.js";
import {
  deletePaseoWorktree,
  isPaseoOwnedWorktreeCwd,
  runWorktreeTeardownCommands,
  WorktreeTeardownError,
} from "../utils/worktree.js";
import type { TerminalManager } from "../terminal/terminal-manager.js";
import type { PersistedWorkspaceRecord, WorkspaceRegistry } from "./workspace-registry.js";
import { createRealpathAwarePathMatcher } from "../utils/path.js";

export type ActiveWorkspaceRef = Pick<
  PersistedWorkspaceRecord,
  "workspaceId" | "cwd" | "kind" | "worktreeRoot" | "isPaseoOwnedWorktree" | "mainRepoRoot"
>;

export type ArchiveMode = "archive_and_cleanup" | "archive_only";

export interface ArchiveDependencies {
  paseoHome?: string;
  // Base directory that may hold worktrees across repositories.
  paseoWorktreesBaseRoot?: string;
  github: ForgeService;
  workspaceGitService: Pick<WorkspaceGitService, "getSnapshot">;
  agentManager: Pick<AgentManager, "listAgents" | "archiveAgent" | "archiveSnapshot">;
  agentStorage: Pick<AgentStorage, "list">;
  // Resolves the worktree at a path to its workspaceId for archive-by-path. The
  // path uniquely identifies a worktree workspace; this is a directory lookup for
  // the archive target, not status/ownership.
  findWorkspaceIdForCwd: (cwd: string) => Promise<string | null>;
  // Active (non-archived) workspaces, used to decide whether the workspace being
  // archived is the last reference to its backing worktree directory, and to
  // break a same-cwd tie in favor of the worktree-kind record when archiving by
  // path (no explicit workspaceId).
  listActiveWorkspaces: () => Promise<ActiveWorkspaceRef[]>;
  archiveWorkspaceRecord: (workspaceId: string) => Promise<void>;
  emitWorkspaceUpdatesForWorkspaceIds: (workspaceIds: Iterable<string>) => Promise<void>;
  markWorkspaceArchiving: (workspaceIds: Iterable<string>, archivingAt: string) => void;
  clearWorkspaceArchiving: (workspaceIds: Iterable<string>) => void;
  killTerminalsForWorkspace: (workspaceId: string) => Promise<void>;
  sessionLogger?: Logger;
}

export interface KillTerminalsForWorkspaceDependencies {
  detachTerminalStream?: (terminalId: string, options: { emitExit: boolean }) => void;
  sessionLogger: Logger;
  terminalManager: TerminalManager | null;
}

export type ArchiveScope =
  | { kind: "workspace"; workspaceId: string }
  | { kind: "worktree"; targetPath: string };

export type ArchiveCleanupStatus = "removed" | "retained" | "failed";

export interface ArchiveCleanup {
  status: ArchiveCleanupStatus;
  reason?: string;
}

export interface ArchiveResult {
  archivedAgentIds: string[];
  archivedWorkspaceIds: string[];
  removedDirectory: boolean;
  cleanup?: ArchiveCleanup;
}

export interface ArchiveByScopeRequest {
  scope: ArchiveScope;
  requestId: string;
  mode?: ArchiveMode;
  /**
   * Existing placement for a workspace-id scope whose record is already
   * archived: lets a retry clean up a residual directory without unarchiving
   * or duplicating the record.
   */
  existingPlacement?: ActiveWorkspaceRef;
  /**
   * Auto-archive supplies this fail-closed policy. Interactive archive keeps
   * its existing force-removal behavior after explicit user confirmation.
   */
  safeWorktreeRemoval?: {
    validateBeforeDelete: (targetPath: string) => Promise<boolean>;
  };
}

interface BackingDirectory {
  path: string;
  isPaseoOwnedWorktree: boolean;
  mainRepoRoot: string | null;
  paseoWorktreesRoot: string | null;
}

interface ArchiveTarget {
  backing: BackingDirectory | null;
  teardownTargets: Array<{ workspaceId: string | null; cwd: string }>;
  workspaceIds: string[];
}

export async function resolveWorkspaceIdAtPath(
  dependencies: Pick<ArchiveDependencies, "findWorkspaceIdForCwd" | "listActiveWorkspaces">,
  targetPath: string,
): Promise<string | null> {
  const matchesTarget = createRealpathAwarePathMatcher(targetPath);
  const activeWorkspaces = await dependencies.listActiveWorkspaces();
  const exactMatches = activeWorkspaces.filter((workspace) => matchesTarget(workspace.cwd));
  const worktreeMatch = exactMatches.find((workspace) => workspace.kind === "worktree");
  if (worktreeMatch) {
    return worktreeMatch.workspaceId;
  }
  return dependencies.findWorkspaceIdForCwd(targetPath);
}

// Resolves the in-scope record set, tears each down
// (agents + terminals + record), then removes the backing directory iff it is
// Paseo-owned AND no active workspace still references it.
export async function archiveByScope(
  dependencies: ArchiveDependencies,
  request: ArchiveByScopeRequest,
): Promise<ArchiveResult> {
  const mode = request.mode ?? "archive_and_cleanup";
  const target = await resolveArchiveTarget(dependencies, request.scope, request.existingPlacement);
  const targetWorkspaceIds = target.workspaceIds;

  if (targetWorkspaceIds.length > 0) {
    dependencies.markWorkspaceArchiving(targetWorkspaceIds, new Date().toISOString());
  }

  let cleanup: ArchiveCleanup = target.backing
    ? { status: "retained", reason: "no eligible managed directory" }
    : { status: "retained", reason: "no backing directory" };

  try {
    if (targetWorkspaceIds.length > 0) {
      await dependencies.emitWorkspaceUpdatesForWorkspaceIds(targetWorkspaceIds);
    }

    const { archivedAgents, archivedWorkspaceIds } = await archiveTargetRecords(
      dependencies,
      targetWorkspaceIds,
      request.requestId,
    );

    const retryWorkspaceIds =
      targetWorkspaceIds.length === 0 && request.existingPlacement
        ? [request.existingPlacement.workspaceId]
        : [];
    const retryResults = await Promise.allSettled(
      retryWorkspaceIds.map((workspaceId) => archiveWorkspaceContents(dependencies, workspaceId)),
    );
    for (const result of retryResults) {
      if (result.status === "fulfilled") {
        for (const agentId of result.value) archivedAgents.add(agentId);
      }
    }
    const teardownFailed =
      archivedWorkspaceIds.length !== targetWorkspaceIds.length ||
      retryResults.some((result) => result.status === "rejected");

    if (target.backing?.mainRepoRoot && targetWorkspaceIds.length > 0) {
      // Best-effort refresh of the main-repo snapshot; must never block record
      // or directory cleanup.
      void (async () => {
        try {
          await dependencies.workspaceGitService.getSnapshot(
            target.backing?.mainRepoRoot as string,
            {
              force: true,
              reason: "archive-worktree",
            },
          );
        } catch (error) {
          dependencies.sessionLogger?.warn(
            { err: error, cwd: target.backing?.mainRepoRoot, requestId: request.requestId },
            "Failed to force-refresh workspace git snapshot after archiving",
          );
        }
      })();
    }

    if (teardownFailed) {
      cleanup = { status: "retained", reason: "teardown failure" };
    } else if (mode === "archive_only") {
      cleanup = {
        status: "retained",
        reason:
          target.backing !== null ? "archive-only request" : "archive-only request; no directory",
      };
    } else if (target.backing !== null) {
      cleanup = await cleanupBackingDirectory(dependencies, request, target, [
        ...archivedWorkspaceIds,
        ...retryWorkspaceIds,
      ]);
    }

    return {
      archivedAgentIds: Array.from(archivedAgents),
      archivedWorkspaceIds,
      removedDirectory: cleanup.status === "removed",
      cleanup,
    };
  } finally {
    if (targetWorkspaceIds.length > 0) {
      dependencies.clearWorkspaceArchiving(targetWorkspaceIds);
      await dependencies.emitWorkspaceUpdatesForWorkspaceIds(targetWorkspaceIds);
    }
  }
}

async function resolveArchiveTarget(
  dependencies: ArchiveDependencies,
  scope: ArchiveScope,
  existingPlacement?: ActiveWorkspaceRef,
): Promise<ArchiveTarget> {
  const activeWorkspaces = await dependencies.listActiveWorkspaces();

  if (scope.kind === "workspace") {
    const workspaceId = scope.workspaceId;
    const record = activeWorkspaces.find((workspace) => workspace.workspaceId === workspaceId);
    if (record) {
      return {
        backing: await resolveWorkspaceBackingDirectory(record, dependencies),
        teardownTargets: [{ workspaceId, cwd: record.cwd }],
        workspaceIds: [workspaceId],
      };
    }
    // Already-archived retry: the resolver only sees active records, so reuse
    // the caller's known placement to finish residual directory cleanup
    // without unarchiving or duplicating the record.
    if (existingPlacement && existingPlacement.workspaceId === workspaceId) {
      return {
        backing: await resolveWorkspaceBackingDirectory(existingPlacement, dependencies),
        teardownTargets: [{ workspaceId, cwd: existingPlacement.cwd }],
        workspaceIds: [],
      };
    }
    dependencies.sessionLogger?.warn(
      { workspaceId },
      "Workspace not found for archive-by-scope; skipping",
    );
    return { backing: null, teardownTargets: [], workspaceIds: [] };
  }

  const backing = await resolveBackingDirectory(scope.targetPath, dependencies);
  const matchesBackingDirectory = createRealpathAwarePathMatcher(backing.path);
  const targetWorkspaces = (
    await Promise.all(
      activeWorkspaces.map(async (workspace) => {
        const backingDirectory = await resolveWorkspaceBackingDirectory(workspace, dependencies);
        return matchesBackingDirectory(backingDirectory.path) ? workspace : null;
      }),
    )
  ).filter((workspace): workspace is ActiveWorkspaceRef => workspace !== null);
  const persistedMainRepoRoot = targetWorkspaces.find(
    (workspace) => workspace.mainRepoRoot,
  )?.mainRepoRoot;
  return {
    backing: {
      ...backing,
      mainRepoRoot: persistedMainRepoRoot ?? backing.mainRepoRoot,
    },
    teardownTargets:
      targetWorkspaces.length > 0
        ? targetWorkspaces.map((workspace) => ({
            workspaceId: workspace.workspaceId,
            cwd: workspace.cwd,
          }))
        : [{ workspaceId: null, cwd: scope.targetPath }],
    workspaceIds: targetWorkspaces.map((workspace) => workspace.workspaceId),
  };
}

async function resolveWorkspaceBackingDirectory(
  workspace: ActiveWorkspaceRef,
  dependencies: Pick<ArchiveDependencies, "paseoHome" | "paseoWorktreesBaseRoot">,
): Promise<BackingDirectory> {
  if (workspace.isPaseoOwnedWorktree && workspace.worktreeRoot && workspace.mainRepoRoot) {
    return {
      path: resolve(workspace.worktreeRoot),
      isPaseoOwnedWorktree: true,
      mainRepoRoot: workspace.mainRepoRoot,
      paseoWorktreesRoot: dirname(resolve(workspace.worktreeRoot)),
    };
  }
  if (workspace.kind !== "worktree") {
    return {
      path: resolve(workspace.cwd),
      isPaseoOwnedWorktree: false,
      mainRepoRoot: workspace.mainRepoRoot ?? null,
      paseoWorktreesRoot: null,
    };
  }

  // COMPAT(archiveMissingWorkspacePlacement): worktree records created before v0.1.110
  // lack durable backing ownership; remove filesystem discovery after 2027-01-17.
  const backing = await resolveBackingDirectory(
    workspace.worktreeRoot ?? workspace.cwd,
    dependencies,
  );
  return { ...backing, mainRepoRoot: workspace.mainRepoRoot ?? backing.mainRepoRoot };
}

async function resolveBackingDirectory(
  cwd: string,
  dependencies: Pick<ArchiveDependencies, "paseoHome" | "paseoWorktreesBaseRoot">,
): Promise<BackingDirectory> {
  const options = {
    paseoHome: dependencies.paseoHome,
    worktreesRoot: dependencies.paseoWorktreesBaseRoot,
  };
  const ownership = await isPaseoOwnedWorktreeCwd(cwd, options);
  return {
    path: resolve(ownership.allowed && ownership.worktreePath ? ownership.worktreePath : cwd),
    isPaseoOwnedWorktree: ownership.allowed,
    mainRepoRoot: ownership.repoRoot ?? null,
    paseoWorktreesRoot: ownership.worktreeRoot ?? null,
  };
}

async function archiveTargetRecords(
  dependencies: ArchiveDependencies,
  targetWorkspaceIds: string[],
  requestId: string,
): Promise<{ archivedAgents: Set<string>; archivedWorkspaceIds: string[] }> {
  const archivedAgents = new Set<string>();
  const archivedWorkspaceIds: string[] = [];

  const results = await Promise.allSettled(
    targetWorkspaceIds.map(async (workspaceId) => {
      const agents = await archiveWorkspaceContents(dependencies, workspaceId);
      await dependencies.archiveWorkspaceRecord(workspaceId);
      return { workspaceId, agents };
    }),
  );

  for (const result of results) {
    if (result.status === "fulfilled") {
      archivedWorkspaceIds.push(result.value.workspaceId);
      for (const agentId of result.value.agents) {
        archivedAgents.add(agentId);
      }
    } else {
      dependencies.sessionLogger?.warn(
        { err: result.reason, requestId },
        "archiveByScope workspace teardown failed; continuing",
      );
    }
  }

  return { archivedAgents, archivedWorkspaceIds };
}

async function cleanupBackingDirectory(
  dependencies: ArchiveDependencies,
  request: Pick<ArchiveByScopeRequest, "requestId" | "safeWorktreeRemoval">,
  target: ArchiveTarget,
  archivedWorkspaceIds: string[],
): Promise<ArchiveCleanup> {
  const backing = target.backing;
  if (!backing) {
    return { status: "retained", reason: "no backing directory" };
  }
  if (!backing.isPaseoOwnedWorktree) {
    return { status: "retained", reason: "external directory" };
  }

  const archivedWorkspaceIdSet = new Set(archivedWorkspaceIds);
  const teardownCwds = uniqueFilesystemPaths(
    target.teardownTargets
      .filter(
        (teardownTarget) =>
          teardownTarget.workspaceId === null ||
          archivedWorkspaceIdSet.has(teardownTarget.workspaceId),
      )
      .map((teardownTarget) => teardownTarget.cwd),
  );

  try {
    for (const teardownCwd of teardownCwds) {
      await runWorktreeTeardownCommands({
        worktreePath: backing.path,
        teardownCwd,
        repoRootPath: backing.mainRepoRoot ?? undefined,
      });
    }
  } catch (error) {
    if (error instanceof WorktreeTeardownError) {
      dependencies.sessionLogger?.warn(
        { err: error, targetPath: backing.path, requestId: request.requestId },
        "Worktree teardown failed during archive; workspace already archived",
      );
      return { status: "retained", reason: "teardown failure" };
    }
    throw error;
  }

  // Recheck active siblings after teardown: an archive_only sibling or a record
  // archived elsewhere still blocks removal. Same existing eligible-directory
  // policy; no new deletion paths.
  const remainingActive = await dependencies.listActiveWorkspaces();
  if (
    !(await isDirectoryUnreferenced(
      remainingActive,
      backing.path,
      new Set(archivedWorkspaceIds),
      dependencies,
    ))
  ) {
    return { status: "retained", reason: "sibling workspace" };
  }

  if (request.safeWorktreeRemoval) {
    try {
      if (!(await request.safeWorktreeRemoval.validateBeforeDelete(backing.path))) {
        dependencies.sessionLogger?.warn(
          { targetPath: backing.path, requestId: request.requestId },
          "Safe worktree removal validation failed; preserving directory",
        );
        return { status: "retained", reason: "failed safe-removal validation" };
      }
    } catch (error) {
      dependencies.sessionLogger?.warn(
        { err: error, targetPath: backing.path, requestId: request.requestId },
        "Safe worktree removal validation errored; preserving directory",
      );
      return { status: "retained", reason: "failed safe-removal validation" };
    }
  }

  try {
    await deletePaseoWorktree({
      cwd: backing.mainRepoRoot,
      worktreePath: backing.path,
      teardownCwds: [],
      worktreesRoot: backing.paseoWorktreesRoot ?? undefined,
      paseoHome: dependencies.paseoHome,
      worktreesBaseRoot: dependencies.paseoWorktreesBaseRoot,
      force: request.safeWorktreeRemoval === undefined,
    });
    dependencies.github.invalidate({ cwd: backing.path });
    return { status: "removed" };
  } catch (error) {
    // Disk failure after a successful record archive keeps archivedAt and
    // reports cleanup failed; the record is never resurrected on clients.
    dependencies.sessionLogger?.warn(
      { err: error, targetPath: backing.path, requestId: request.requestId },
      "Worktree disk removal failed during archive; workspace already archived",
    );
    return { status: "failed", reason: "directory removal failed" };
  }
}

function uniqueFilesystemPaths(paths: string[]): string[] {
  const unique: string[] = [];
  for (const candidate of paths) {
    if (!unique.some((existing) => createRealpathAwarePathMatcher(existing)(candidate))) {
      unique.push(candidate);
    }
  }
  return unique;
}

export type ArchiveWorkspaceContentsDependencies = Pick<
  ArchiveDependencies,
  "agentManager" | "agentStorage" | "killTerminalsForWorkspace" | "sessionLogger"
>;

// Tears down everything OWNED by a single workspace record: its live agents,
// its persisted-but-not-running agent snapshots, and its terminals. Scoped by
// workspaceId so a sibling workspace sharing the same directory is untouched.
// Required live-agent/terminal teardown failures throw so callers never remove
// the backing directory after a failed teardown. Provider-owned native history
// stays best-effort inside agentManager. Returns the set of archived agent ids.
export async function archiveWorkspaceContents(
  dependencies: ArchiveWorkspaceContentsDependencies,
  workspaceId: string,
): Promise<Set<string>> {
  const archivedAgents = new Set<string>();

  const liveAgents = dependencies.agentManager
    .listAgents()
    .filter((agent) => agent.workspaceId === workspaceId);
  for (const agent of liveAgents) {
    archivedAgents.add(agent.id);
  }

  const storedRecords = await dependencies.agentStorage.list();
  const liveAgentIds = new Set(liveAgents.map((agent) => agent.id));
  const matchingStoredRecords = storedRecords.filter(
    (record) => record.workspaceId === workspaceId,
  );
  for (const record of matchingStoredRecords) {
    archivedAgents.add(record.id);
  }

  const archivedAt = new Date().toISOString();
  const teardownResults = await Promise.allSettled([
    ...liveAgents.map((agent) => dependencies.agentManager.archiveAgent(agent.id)),
    dependencies.killTerminalsForWorkspace(workspaceId),
  ]);
  const teardownFailures = teardownResults.filter(
    (result): result is PromiseRejectedResult => result.status === "rejected",
  );
  if (teardownFailures.length > 0) {
    throw new AggregateError(
      teardownFailures.map((result) => result.reason),
      "Required workspace teardown failed",
    );
  }

  const snapshotResults = await Promise.allSettled(
    matchingStoredRecords
      .filter((record) => !liveAgentIds.has(record.id) && !record.archivedAt)
      .map((record) => dependencies.agentManager.archiveSnapshot(record.id, archivedAt)),
  );
  const snapshotFailures = snapshotResults.filter(
    (result): result is PromiseRejectedResult => result.status === "rejected",
  );
  if (snapshotFailures.length > 0) {
    throw new AggregateError(
      snapshotFailures.map((result) => result.reason),
      "Stored workspace agents could not be archived",
    );
  }

  return archivedAgents;
}

// True when, after archiving
// the in-scope records, no active workspace still points at targetDir. Derived
// from records each call — no stored counter.
async function isDirectoryUnreferenced(
  activeWorkspaces: ActiveWorkspaceRef[],
  targetDir: string,
  archivedWorkspaceIds: ReadonlySet<string>,
  dependencies: Pick<ArchiveDependencies, "paseoHome" | "paseoWorktreesBaseRoot">,
): Promise<boolean> {
  const target = resolve(targetDir);
  const matchesTarget = createRealpathAwarePathMatcher(target);
  for (const workspace of activeWorkspaces) {
    if (archivedWorkspaceIds.has(workspace.workspaceId)) continue;
    const backingDirectory = await resolveWorkspaceBackingDirectory(workspace, dependencies);
    if (matchesTarget(backingDirectory.path)) return false;
  }
  return true;
}

export async function killTerminalsForWorkspace(
  dependencies: KillTerminalsForWorkspaceDependencies,
  workspaceId: string,
): Promise<void> {
  const terminalManager = dependencies.terminalManager;
  if (!terminalManager) {
    return;
  }

  const terminalIds: string[] = [];
  const terminalLists = await Promise.all(
    terminalManager.listDirectories().map(async (terminalCwd) => {
      try {
        return await terminalManager.getTerminals(terminalCwd, { workspaceId });
      } catch (error) {
        throw new Error(
          `Failed to enumerate workspace terminals during archive: ${error instanceof Error ? error.message : String(error)}`,
          { cause: error },
        );
      }
    }),
  );
  for (const terminals of terminalLists) {
    for (const terminal of terminals) {
      if (terminal.workspaceId === workspaceId) {
        terminalIds.push(terminal.id);
      }
    }
  }

  if (terminalIds.length === 0) {
    return;
  }

  const killResults = await Promise.allSettled(
    terminalIds.map(async (terminalId) => {
      dependencies.detachTerminalStream?.(terminalId, { emitExit: true });
      await terminalManager.killTerminalAndWait(terminalId, {
        gracefulTimeoutMs: 2000,
        forceTimeoutMs: 1500,
      });
    }),
  );
  const failures = killResults.filter(
    (result): result is PromiseRejectedResult => result.status === "rejected",
  );
  if (failures.length > 0) {
    for (const failure of failures) {
      dependencies.sessionLogger.warn(
        { err: failure.reason, workspaceId },
        "Workspace terminal teardown failed during archive",
      );
    }
    const first = failures[0]?.reason;
    throw first instanceof Error
      ? first
      : new Error(
          `Failed to kill ${String(failures.length)} terminal(s) for workspace ${workspaceId}`,
        );
  }
}

// Archiving the last workspace of a project leaves the project record active.
// The user removes the project explicitly, so we never archive the parent here.
export async function archivePersistedWorkspaceRecord(input: {
  workspaceId: string;
  workspaceRegistry: Pick<WorkspaceRegistry, "get" | "archive">;
  archivedAt?: string;
}): Promise<PersistedWorkspaceRecord | null> {
  const existingWorkspace = await input.workspaceRegistry.get(input.workspaceId);
  if (!existingWorkspace) {
    return null;
  }

  if (existingWorkspace.archivedAt) {
    return existingWorkspace;
  }

  const archivedAt = input.archivedAt ?? new Date().toISOString();
  await input.workspaceRegistry.archive(input.workspaceId, archivedAt);

  return existingWorkspace;
}
