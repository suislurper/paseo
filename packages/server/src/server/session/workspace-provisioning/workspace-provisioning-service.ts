import { basename, resolve } from "node:path";
import type { Logger } from "pino";
import {
  generateWorkspaceId,
  initialWorkspacePlacement,
  reconcileWorkspacePlacement,
} from "../../workspace-registry-model.js";
import {
  createPersistedWorkspaceRecord,
  type PersistedProjectRecord,
  type PersistedWorkspaceRecord,
  type ProjectRegistry,
  type WorkspaceRegistry,
} from "../../workspace-registry.js";
import type { WorkspaceGitService } from "../../workspace-git-service.js";
import type { CreatePaseoWorktreeWorkflowResult } from "../../worktree-session.js";
import { areEquivalentPaths, createRealpathAwarePathMatcher } from "../../../utils/path.js";
import {
  openDirectoryWorkspaceLaunch,
  retainDirectoryWorkspaceLaunch as retainOwnedDirectoryWorkspaceLaunch,
  type DirectoryWorkspaceLaunch,
  type DirectoryWorkspaceLaunchReferenceInspection,
} from "./directory-workspace-launch-cleanup.js";

export type { DirectoryWorkspaceLaunch, DirectoryWorkspaceLaunchReferenceInspection };

export interface ResolveOrCreateWorkspaceIdInput {
  createdWorktree: CreatePaseoWorktreeWorkflowResult | null;
  requestedWorkspaceId?: string;
  cwd: string;
  initialTitle: string | null;
}

export interface ImportWorkspaceInput {
  cwd: string;
  requestedWorkspaceId?: string;
}

export interface ImportWorkspaceResult<T> {
  value: T;
  createdWorkspace: PersistedWorkspaceRecord | null;
}

export interface CreateWorktreeWorkspaceInput {
  sourceCwd: string;
  projectId?: string;
  repoRoot: string;
  cwd: string;
  worktreeRoot: string;
  branch: string | null;
  baseBranch: string | null;
  title: string | null;
}

export interface AllocateDirectoryWorkspaceForLaunchInput {
  cwd: string;
  title?: string | null;
  projectId?: string;
}

export interface WorkspaceProvisioningService {
  runInImportWorkspace<T>(
    input: ImportWorkspaceInput,
    operation: (workspace: PersistedWorkspaceRecord) => Promise<T>,
  ): Promise<ImportWorkspaceResult<T>>;
  findOrCreateWorkspaceForDirectory(cwd: string): Promise<PersistedWorkspaceRecord>;
  resolveOrCreateWorkspaceIdForCreateAgent(input: ResolveOrCreateWorkspaceIdInput): Promise<string>;
  createWorkspaceForDirectory(
    cwd: string,
    title?: string | null,
    projectId?: string,
  ): Promise<PersistedWorkspaceRecord>;
  createWorkspaceForWorktree(
    input: CreateWorktreeWorkspaceInput,
  ): Promise<PersistedWorkspaceRecord>;
  findOrCreateProjectForDirectory(cwd: string): Promise<PersistedProjectRecord>;
  ensureWorkspaceRecordUnarchived(
    workspace: PersistedWorkspaceRecord,
  ): Promise<PersistedWorkspaceRecord>;
  /**
   * Fresh directory workspace for one launch request. Ownership is registered
   * before the record is discoverable. Success: `retain()` then `commit()`.
   * Failure: `await cleanupUnusedOnFailure()` (never throws) then rethrow.
   * Independent attaches: `retainDirectoryWorkspaceLaunch(workspaceId)`.
   */
  allocateDirectoryWorkspaceForLaunch(
    input: AllocateDirectoryWorkspaceForLaunchInput,
  ): Promise<DirectoryWorkspaceLaunch>;
  retainDirectoryWorkspaceLaunch(workspaceId: string): void;
  requireExistingWorkspaceForLaunch(workspaceId: string): Promise<PersistedWorkspaceRecord>;
}

export type WorkspaceProvisioningErrorCode =
  | "unknown_project"
  | "archived_project"
  | "unknown_workspace"
  | "archived_workspace";

export class WorkspaceProvisioningError extends Error {
  constructor(
    readonly code: WorkspaceProvisioningErrorCode,
    id: string,
  ) {
    super(workspaceProvisioningErrorMessage(code, id));
    this.name = "WorkspaceProvisioningError";
  }
}

function workspaceProvisioningErrorMessage(
  code: WorkspaceProvisioningErrorCode,
  id: string,
): string {
  switch (code) {
    case "unknown_project":
      return `Unknown project: ${id}`;
    case "archived_project":
      return `Archived project: ${id}`;
    case "unknown_workspace":
      return `Unknown workspace: ${id}`;
    case "archived_workspace":
      return `Archived workspace: ${id}`;
  }
}

export function createWorkspaceProvisioningService(deps: {
  workspaceRegistry: WorkspaceRegistry;
  projectRegistry: ProjectRegistry;
  workspaceGitService: Pick<WorkspaceGitService, "getCheckout" | "peekSnapshot">;
  logger: Logger;
  inspectDirectoryWorkspaceLaunchReferences?: (
    workspaceId: string,
  ) => Promise<DirectoryWorkspaceLaunchReferenceInspection>;
}): WorkspaceProvisioningService {
  const {
    workspaceRegistry,
    projectRegistry,
    workspaceGitService,
    logger,
    inspectDirectoryWorkspaceLaunchReferences,
  } = deps;

  async function runInImportWorkspace<T>(
    input: ImportWorkspaceInput,
    operation: (workspace: PersistedWorkspaceRecord) => Promise<T>,
  ): Promise<ImportWorkspaceResult<T>> {
    if (input.requestedWorkspaceId) {
      const workspace = await workspaceRegistry.get(input.requestedWorkspaceId);
      if (!workspace || workspace.archivedAt) {
        throw new Error(`Workspace not found: ${input.requestedWorkspaceId}`);
      }
      const project = await projectRegistry.get(workspace.projectId);
      if (!project || project.archivedAt) {
        throw new Error(`Project not found: ${workspace.projectId}`);
      }
      if (!createRealpathAwarePathMatcher(workspace.cwd)(input.cwd)) {
        throw new Error(`Import cwd does not match workspace: ${workspace.workspaceId}`);
      }
      return {
        value: await operation(workspace),
        createdWorkspace: null,
      };
    }

    const projectsBeforeImport = await projectRegistry.list();
    const workspace = await createWorkspaceForDirectory(input.cwd);
    const previousProject =
      projectsBeforeImport.find((project) => project.projectId === workspace.projectId) ?? null;

    try {
      return {
        value: await operation(workspace),
        createdWorkspace: workspace,
      };
    } catch (error) {
      await rollbackFailedImportWorkspace(workspace, previousProject);
      throw error;
    }
  }

  async function rollbackFailedImportWorkspace(
    workspace: PersistedWorkspaceRecord,
    previousProject: PersistedProjectRecord | null,
  ): Promise<void> {
    try {
      await workspaceRegistry.remove(workspace.workspaceId);
      const projectHasActiveWorkspace = (await workspaceRegistry.list()).some(
        (candidate) => candidate.projectId === workspace.projectId && !candidate.archivedAt,
      );
      if (projectHasActiveWorkspace) {
        return;
      }
      if (previousProject?.archivedAt) {
        await projectRegistry.upsert(previousProject);
      } else if (!previousProject) {
        await projectRegistry.remove(workspace.projectId);
      }
    } catch (error) {
      logger.error(
        { err: error, workspaceId: workspace.workspaceId, projectId: workspace.projectId },
        "Failed to restore workspace state after provider import failure",
      );
    }
  }

  async function findOrCreateProjectForDirectory(cwd: string): Promise<PersistedProjectRecord> {
    const rootPath = resolve(cwd);
    const checkout = await workspaceGitService.getCheckout(rootPath);
    const timestamp = new Date().toISOString();
    return projectRegistry.getOrCreateActiveByRoot({
      rootPath,
      kind: checkout.isGit ? "git" : "non_git",
      displayName: basename(rootPath) || rootPath,
      timestamp,
    });
  }

  async function requireActiveProject(projectId: string): Promise<PersistedProjectRecord> {
    const project = await projectRegistry.get(projectId);
    if (!project) throw new WorkspaceProvisioningError("unknown_project", projectId);
    if (project.archivedAt) throw new WorkspaceProvisioningError("archived_project", projectId);
    return project;
  }

  async function buildDirectoryWorkspaceRecord(
    cwd: string,
    title?: string | null,
    projectId?: string,
  ): Promise<PersistedWorkspaceRecord> {
    const normalizedCwd = resolve(cwd);
    const checkout = await workspaceGitService.getCheckout(normalizedCwd);
    const project = projectId
      ? await refreshProjectKind(await requireActiveProject(projectId), normalizedCwd, checkout)
      : // COMPAT(workspaceCreateMissingProjectId): added in v0.1.107, remove after 2027-01-15.
        await findOrCreateProjectForDirectory(normalizedCwd);
    const timestamp = new Date().toISOString();
    return createPersistedWorkspaceRecord({
      workspaceId: generateWorkspaceId(),
      projectId: project.projectId,
      ...initialWorkspacePlacement({ source: "checkout", cwd: normalizedCwd, checkout }),
      title: title?.trim() || null,
      createdAt: timestamp,
      updatedAt: timestamp,
    });
  }

  async function createWorkspaceForDirectory(
    cwd: string,
    title?: string | null,
    projectId?: string,
  ): Promise<PersistedWorkspaceRecord> {
    const workspace = await buildDirectoryWorkspaceRecord(cwd, title, projectId);
    await workspaceRegistry.upsert(workspace);
    return workspace;
  }

  async function allocateDirectoryWorkspaceForLaunch(
    input: AllocateDirectoryWorkspaceForLaunchInput,
  ): Promise<DirectoryWorkspaceLaunch> {
    const workspace = await buildDirectoryWorkspaceRecord(input.cwd, input.title, input.projectId);
    const launch = openDirectoryWorkspaceLaunch({
      workspace,
      workspaceRegistry,
      logger,
      inspectReferences: inspectDirectoryWorkspaceLaunchReferences,
    });
    try {
      await workspaceRegistry.upsert(workspace);
    } catch (error) {
      const leftover = await workspaceRegistry.get(workspace.workspaceId);
      if (!leftover) {
        launch.commit();
        throw error;
      }
      logger.error(
        { err: error, workspaceId: workspace.workspaceId },
        "Directory workspace allocation persist failed after cache mutation",
      );
    }
    return launch;
  }

  function retainDirectoryWorkspaceLaunch(workspaceId: string): void {
    retainOwnedDirectoryWorkspaceLaunch(workspaceRegistry, workspaceId);
  }

  async function requireExistingWorkspaceForLaunch(
    workspaceId: string,
  ): Promise<PersistedWorkspaceRecord> {
    const workspace = await workspaceRegistry.get(workspaceId);
    if (!workspace) throw new WorkspaceProvisioningError("unknown_workspace", workspaceId);
    if (workspace.archivedAt) {
      throw new WorkspaceProvisioningError("archived_workspace", workspaceId);
    }
    return workspace;
  }

  async function createWorkspaceForWorktree(
    input: CreateWorktreeWorkspaceInput,
  ): Promise<PersistedWorkspaceRecord> {
    const sourceCwd = resolve(input.sourceCwd);
    const repoRoot = resolve(input.repoRoot);
    const cwd = resolve(input.cwd);
    const worktreeRoot = resolve(input.worktreeRoot);
    const project = await resolveSourceProjectForWorktree({
      sourceCwd,
      projectId: input.projectId,
      repoRoot,
    });
    const timestamp = new Date().toISOString();
    const workspace = createPersistedWorkspaceRecord({
      workspaceId: generateWorkspaceId(),
      projectId: project.projectId,
      ...initialWorkspacePlacement({
        source: "created_worktree",
        cwd,
        worktreeRoot,
        branch: input.branch,
        baseBranch: input.baseBranch,
        mainRepoRoot: repoRoot,
      }),
      title: input.title,
      createdAt: timestamp,
      updatedAt: timestamp,
    });
    await workspaceRegistry.upsert(workspace);
    return workspace;
  }

  async function resolveSourceProjectForWorktree(input: {
    sourceCwd: string;
    projectId?: string;
    repoRoot: string;
  }): Promise<PersistedProjectRecord> {
    if (input.projectId) {
      return refreshProjectKind(await requireActiveProject(input.projectId));
    }

    const workspaces = await workspaceRegistry.list();
    const sourceWorkspace =
      workspaces.find(
        (workspace) => !workspace.archivedAt && areEquivalentPaths(workspace.cwd, input.sourceCwd),
      ) ??
      workspaces.find(
        (workspace) => !workspace.archivedAt && areEquivalentPaths(workspace.cwd, input.repoRoot),
      );
    if (sourceWorkspace) {
      const project = await projectRegistry.get(sourceWorkspace.projectId);
      if (project) return refreshProjectKind(project);
      // COMPAT(worktreeMissingSourceProject): added in v0.1.107, remove after 2027-01-15.
      // Orphaned legacy workspace FKs fall through to exact-root allocation.
    }

    const project = await projectRegistry.getOrCreateActiveByRoot({
      rootPath: input.repoRoot,
      kind: "git",
      displayName: basename(input.repoRoot) || input.repoRoot,
      timestamp: new Date().toISOString(),
    });
    return refreshProjectKind(project);
  }

  async function findOrCreateWorkspaceForDirectory(cwd: string): Promise<PersistedWorkspaceRecord> {
    const normalizedCwd = resolve(cwd);
    const workspaces = await workspaceRegistry.list();
    const active = workspaces
      .filter(
        (workspace) => !workspace.archivedAt && areEquivalentPaths(workspace.cwd, normalizedCwd),
      )
      .sort(
        (left, right) =>
          Date.parse(left.createdAt) - Date.parse(right.createdAt) ||
          left.workspaceId.localeCompare(right.workspaceId),
      )[0];
    if (active) return refreshWorkspaceRecord(active);
    const archived = workspaces
      .filter(
        (workspace) => workspace.archivedAt && areEquivalentPaths(workspace.cwd, normalizedCwd),
      )
      .sort(
        (left, right) =>
          Date.parse(left.createdAt) - Date.parse(right.createdAt) ||
          left.workspaceId.localeCompare(right.workspaceId),
      )[0];
    if (archived) {
      const project = await projectRegistry.get(archived.projectId);
      if (project && !project.archivedAt) return ensureWorkspaceRecordUnarchived(archived);
    }
    return createWorkspaceForDirectory(normalizedCwd);
  }

  async function resolveOrCreateWorkspaceIdForCreateAgent(
    input: ResolveOrCreateWorkspaceIdInput,
  ): Promise<string> {
    if (input.createdWorktree) return input.createdWorktree.workspace.workspaceId;
    if (input.requestedWorkspaceId) return input.requestedWorkspaceId;
    return (await createWorkspaceForDirectory(input.cwd, input.initialTitle)).workspaceId;
  }

  async function ensureWorkspaceRecordUnarchived(
    workspace: PersistedWorkspaceRecord,
  ): Promise<PersistedWorkspaceRecord> {
    const project = await projectRegistry.get(workspace.projectId);
    if (!project) throw new Error(`Unknown project: ${workspace.projectId}`);
    const timestamp = new Date().toISOString();
    const checkout =
      workspace.archivedAt || project.archivedAt
        ? await workspaceGitService.getCheckout(workspace.cwd)
        : null;
    let next: PersistedWorkspaceRecord | null = null;
    if (workspace.archivedAt && checkout) {
      const placementUpdate = reconcileWorkspacePlacement({
        workspace,
        checkout,
        updatedAt: timestamp,
      });
      next = {
        ...(placementUpdate?.workspace ?? workspace),
        archivedAt: null,
        updatedAt: timestamp,
      };
    }
    if (checkout && (project.archivedAt || workspace.archivedAt)) {
      const projectCheckout = areEquivalentPaths(project.rootPath, workspace.cwd)
        ? checkout
        : await workspaceGitService.getCheckout(project.rootPath);
      const kind = projectCheckout.isGit ? "git" : "non_git";
      if (project.archivedAt || project.kind !== kind) {
        await projectRegistry.upsert({ ...project, kind, archivedAt: null, updatedAt: timestamp });
      }
    }
    if (!next) return workspace;
    await workspaceRegistry.upsert(next);
    return next;
  }

  async function refreshWorkspaceRecord(
    workspace: PersistedWorkspaceRecord,
  ): Promise<PersistedWorkspaceRecord> {
    const checkout = await workspaceGitService.getCheckout(workspace.cwd);
    const project = await projectRegistry.get(workspace.projectId);
    if (project && !project.archivedAt) {
      await refreshProjectKind(project, workspace.cwd, checkout);
    }
    const update = reconcileWorkspacePlacement({
      workspace,
      checkout,
      updatedAt: new Date().toISOString(),
    });
    if (!update) return workspace;
    await workspaceRegistry.upsert(update.workspace);
    return update.workspace;
  }

  async function refreshProjectKind(
    project: PersistedProjectRecord,
    workspaceCwd?: string,
    workspaceCheckout?: Awaited<ReturnType<WorkspaceGitService["getCheckout"]>>,
  ): Promise<PersistedProjectRecord> {
    const projectCheckout =
      workspaceCwd && workspaceCheckout && areEquivalentPaths(project.rootPath, workspaceCwd)
        ? workspaceCheckout
        : await workspaceGitService.getCheckout(project.rootPath);
    const kind: PersistedProjectRecord["kind"] = projectCheckout.isGit ? "git" : "non_git";
    if (project.kind === kind) return project;
    const refreshed = { ...project, kind, updatedAt: new Date().toISOString() };
    await projectRegistry.upsert(refreshed);
    return refreshed;
  }

  return {
    runInImportWorkspace,
    findOrCreateWorkspaceForDirectory,
    resolveOrCreateWorkspaceIdForCreateAgent,
    createWorkspaceForDirectory,
    createWorkspaceForWorktree,
    findOrCreateProjectForDirectory,
    ensureWorkspaceRecordUnarchived,
    allocateDirectoryWorkspaceForLaunch,
    retainDirectoryWorkspaceLaunch,
    requireExistingWorkspaceForLaunch,
  };
}
