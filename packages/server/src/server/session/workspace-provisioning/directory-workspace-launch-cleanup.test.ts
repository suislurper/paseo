import os from "node:os";
import path from "node:path";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";

import type { Logger } from "pino";
import { afterEach, beforeEach, expect, test } from "vitest";

import { createTestLogger } from "../../../test-utils/test-logger.js";
import { createNoopWorkspaceGitService } from "../../test-utils/workspace-git-service-stub.js";
import {
  FileBackedProjectRegistry,
  FileBackedWorkspaceRegistry,
  type WorkspaceRegistry,
} from "../../workspace-registry.js";
import {
  createWorkspaceProvisioningService,
  WorkspaceProvisioningError,
  type DirectoryWorkspaceLaunchReferenceInspection,
  type WorkspaceProvisioningService,
} from "./workspace-provisioning-service.js";

const unusedInspection: DirectoryWorkspaceLaunchReferenceInspection = {
  available: true,
  hasManagedAgent: false,
  hasPersistedAgent: false,
  hasTerminal: false,
};

let tmpDir: string;
let gitRoots: Set<string>;
let workspaceRegistry: FileBackedWorkspaceRegistry;
let projectRegistry: FileBackedProjectRegistry;
let provisioning: WorkspaceProvisioningService;
let errorLogs: Array<{ bindings: Record<string, unknown>; message: string }>;

function capturingLogger(): Logger {
  return {
    error(bindings: Record<string, unknown>, message: string) {
      errorLogs.push({ bindings, message });
    },
  } as unknown as Logger;
}

function gitService() {
  return createNoopWorkspaceGitService({
    peekSnapshot: () => null,
    getCheckout: async (cwd: string) => {
      let worktreeRoot: string | null = null;
      for (const root of gitRoots) {
        if (
          (cwd === root || cwd.startsWith(`${root}${path.sep}`)) &&
          root.length > (worktreeRoot?.length ?? -1)
        ) {
          worktreeRoot = root;
        }
      }
      return {
        cwd,
        isGit: worktreeRoot !== null,
        currentBranch: worktreeRoot ? "main" : null,
        remoteUrl: null,
        worktreeRoot,
        isPaseoOwnedWorktree: false,
        mainRepoRoot: null,
      };
    },
  });
}

function createProvisioning(options?: {
  workspaceRegistry?: WorkspaceRegistry;
  inspect?: (workspaceId: string) => Promise<DirectoryWorkspaceLaunchReferenceInspection>;
  logger?: Logger;
  workspaceGitService?: ReturnType<typeof createNoopWorkspaceGitService>;
}): WorkspaceProvisioningService {
  return createWorkspaceProvisioningService({
    workspaceRegistry: options?.workspaceRegistry ?? workspaceRegistry,
    projectRegistry,
    workspaceGitService: options?.workspaceGitService ?? gitService(),
    logger: options?.logger ?? capturingLogger(),
    inspectDirectoryWorkspaceLaunchReferences: options?.inspect,
  });
}

function wrapRegistry(overrides: Partial<WorkspaceRegistry>): WorkspaceRegistry {
  return {
    initialize: () => workspaceRegistry.initialize(),
    existsOnDisk: () => workspaceRegistry.existsOnDisk(),
    list: () => workspaceRegistry.list(),
    get: (workspaceId) => workspaceRegistry.get(workspaceId),
    update: (workspaceId, updater) => workspaceRegistry.update(workspaceId, updater),
    upsert: (workspace) => workspaceRegistry.upsert(workspace),
    archive: (workspaceId, archivedAt) => workspaceRegistry.archive(workspaceId, archivedAt),
    remove: (workspaceId) => workspaceRegistry.remove(workspaceId),
    ...overrides,
  };
}

async function expectArchived(workspaceId: string): Promise<void> {
  const record = await workspaceRegistry.get(workspaceId);
  expect(record).toMatchObject({ workspaceId, archivedAt: expect.any(String) });
}

async function expectActive(workspaceId: string): Promise<void> {
  const record = await workspaceRegistry.get(workspaceId);
  expect(record).toMatchObject({ workspaceId, archivedAt: null });
}

beforeEach(async () => {
  tmpDir = mkdtempSync(path.join(os.tmpdir(), "directory-workspace-launch-"));
  gitRoots = new Set();
  errorLogs = [];
  workspaceRegistry = new FileBackedWorkspaceRegistry(
    path.join(tmpDir, "projects", "workspaces.json"),
    createTestLogger(),
  );
  projectRegistry = new FileBackedProjectRegistry(
    path.join(tmpDir, "projects", "projects.json"),
    createTestLogger(),
  );
  await workspaceRegistry.initialize();
  await projectRegistry.initialize();
  provisioning = createProvisioning({ inspect: async () => unusedInspection });
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

test("unused failed directory launch archives only that metadata record and leaves files", async () => {
  const cwd = path.join(tmpDir, "plain");
  mkdirSync(cwd);
  const keptFile = path.join(cwd, "keep.txt");
  writeFileSync(keptFile, "keep me");

  const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({ cwd });
  await launch.cleanupUnusedOnFailure();

  await expectArchived(launch.workspaceId);
  expect(await workspaceRegistry.list()).toHaveLength(1);
  expect((await projectRegistry.get(launch.workspace.projectId))?.archivedAt).toBeNull();
  expect(readFileSync(keptFile, "utf8")).toBe("keep me");
});

test("successful launch commit keeps the directory workspace and later cleanup is a no-op", async () => {
  const cwd = path.join(tmpDir, "success");
  const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({ cwd, title: "Launch" });
  launch.retain();
  launch.commit();
  await launch.cleanupUnusedOnFailure();

  await expectActive(launch.workspaceId);
  expect((await workspaceRegistry.get(launch.workspaceId))?.title).toBe("Launch");
});

test("failed launch cleanup leaves preexisting and same-cwd directory workspaces active", async () => {
  const otherCwd = path.join(tmpDir, "other");
  const cwd = path.join(tmpDir, "shared");
  const preexisting = await provisioning.createWorkspaceForDirectory(otherCwd);
  const sameCwd = await provisioning.createWorkspaceForDirectory(cwd);

  const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({ cwd });
  await launch.cleanupUnusedOnFailure();

  await expectArchived(launch.workspaceId);
  await expectActive(preexisting.workspaceId);
  await expectActive(sameCwd.workspaceId);
  expect(sameCwd.workspaceId).not.toBe(launch.workspaceId);
});

test.each([
  {
    label: "managed agent",
    inspection: {
      available: true as const,
      hasManagedAgent: true,
      hasPersistedAgent: false,
      hasTerminal: false,
    },
  },
  {
    label: "persisted agent",
    inspection: {
      available: true as const,
      hasManagedAgent: false,
      hasPersistedAgent: true,
      hasTerminal: false,
    },
  },
  {
    label: "terminal",
    inspection: {
      available: true as const,
      hasManagedAgent: false,
      hasPersistedAgent: false,
      hasTerminal: true,
    },
  },
])("existing $label reference retains the failed launch workspace", async ({ inspection }) => {
  provisioning = createProvisioning({ inspect: async () => inspection });
  const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({
    cwd: path.join(tmpDir, "referenced"),
  });
  await launch.cleanupUnusedOnFailure();
  await expectActive(launch.workspaceId);
});

test("cleanup failure never throws, logs the workspace id, and leaves the original launch error", async () => {
  const secondary = new Error("registry update failed");
  provisioning = createProvisioning({
    workspaceRegistry: wrapRegistry({
      update: async () => {
        throw secondary;
      },
    }),
    inspect: async () => unusedInspection,
    logger: capturingLogger(),
  });
  const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({
    cwd: path.join(tmpDir, "cleanup-error"),
  });
  const original = new Error("agent create failed");

  let thrown: unknown;
  try {
    try {
      throw original;
    } catch (error) {
      await launch.cleanupUnusedOnFailure();
      throw error;
    }
  } catch (error) {
    thrown = error;
  }

  expect(thrown).toBe(original);
  expect(errorLogs.map((entry) => entry.bindings.workspaceId)).toContain(launch.workspaceId);
  expect(errorLogs.map((entry) => entry.bindings.err)).toContain(secondary);
  await expectActive(launch.workspaceId);
});

test("retain during deferred reference inspection does not archive", async () => {
  let inspectStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    inspectStarted = resolve;
  });
  let releaseInspect!: () => void;
  const inspectGate = new Promise<void>((resolve) => {
    releaseInspect = resolve;
  });
  provisioning = createProvisioning({
    inspect: async () => {
      inspectStarted();
      await inspectGate;
      return unusedInspection;
    },
  });
  const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({
    cwd: path.join(tmpDir, "race"),
  });

  const cleanup = launch.cleanupUnusedOnFailure();
  await started;
  launch.retain();
  releaseInspect();
  await cleanup;

  await expectActive(launch.workspaceId);
});

test("retain before ownership publication cannot get lost", async () => {
  let launchProvisioning!: WorkspaceProvisioningService;
  launchProvisioning = createProvisioning({
    workspaceRegistry: wrapRegistry({
      upsert: async (record) => {
        await workspaceRegistry.upsert(record);
        launchProvisioning.retainDirectoryWorkspaceLaunch(record.workspaceId);
      },
    }),
    inspect: async () => unusedInspection,
  });
  const launch = await launchProvisioning.allocateDirectoryWorkspaceForLaunch({
    cwd: path.join(tmpDir, "pre-publish"),
  });
  await launch.cleanupUnusedOnFailure();
  await expectActive(launch.workspaceId);
});

test("failed worktree-kind allocation is not archived by launch cleanup", async () => {
  const cwd = path.join(tmpDir, "manual-worktree");
  const mainRepoRoot = path.join(tmpDir, "main-repo");
  provisioning = createProvisioning({
    inspect: async () => unusedInspection,
    workspaceGitService: createNoopWorkspaceGitService({
      peekSnapshot: () => null,
      getCheckout: async () => ({
        cwd,
        isGit: true,
        currentBranch: "feature/manual",
        remoteUrl: null,
        worktreeRoot: cwd,
        isPaseoOwnedWorktree: false,
        mainRepoRoot,
      }),
    }),
  });
  const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({ cwd });
  expect(launch.workspace.kind).toBe("worktree");
  await launch.cleanupUnusedOnFailure();
  await expectActive(launch.workspaceId);
});

test("missing inspector preserves the unused launch workspace without logging inspection failure", async () => {
  provisioning = createProvisioning({ inspect: undefined });
  const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({
    cwd: path.join(tmpDir, "fail-safe-missing"),
  });
  await launch.cleanupUnusedOnFailure();
  await expectActive(launch.workspaceId);
  expect(errorLogs).toEqual([]);
});

test.each([
  {
    label: "inspector throws",
    inspect: async () => {
      throw new Error("reference inspection unavailable");
    },
  },
  {
    label: "inspector reports unavailable",
    inspect: async () => ({ available: false as const }),
  },
])(
  "fail-safe $label preserves the unused launch workspace and logs the workspace id",
  async ({ inspect }) => {
    const original = new Error("agent create failed");
    provisioning = createProvisioning({ inspect });
    const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({
      cwd: path.join(tmpDir, "fail-safe"),
    });

    let thrown: unknown;
    try {
      try {
        throw original;
      } catch (error) {
        await launch.cleanupUnusedOnFailure();
        throw error;
      }
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBe(original);
    expect(errorLogs.map((entry) => entry.bindings.workspaceId)).toContain(launch.workspaceId);
    await expectActive(launch.workspaceId);
  },
);

test("allocation persist failure after cache mutation rejects, archives leftover, and keeps the original error", async () => {
  const persistError = new Error("persist failed after cache mutation");
  let leftoverId: string | undefined;
  provisioning = createProvisioning({
    workspaceRegistry: wrapRegistry({
      upsert: async (record) => {
        leftoverId = record.workspaceId;
        await workspaceRegistry.upsert(record);
        throw persistError;
      },
    }),
    inspect: async () => unusedInspection,
    logger: capturingLogger(),
  });

  await expect(
    provisioning.allocateDirectoryWorkspaceForLaunch({
      cwd: path.join(tmpDir, "leftover"),
    }),
  ).rejects.toBe(persistError);

  expect(leftoverId).toEqual(expect.any(String));
  expect(errorLogs.map((entry) => entry.bindings.workspaceId)).toContain(leftoverId);
  expect(errorLogs.map((entry) => entry.bindings.err)).toContain(persistError);
  await expectArchived(leftoverId as string);
});

test("allocation persist failure preserves the original error when leftover lookup fails", async () => {
  const persistError = new Error("persist failed before leftover lookup");
  const lookupError = new Error("leftover lookup failed");
  provisioning = createProvisioning({
    workspaceRegistry: wrapRegistry({
      upsert: async () => {
        throw persistError;
      },
      get: async () => {
        throw lookupError;
      },
    }),
    inspect: async () => unusedInspection,
    logger: capturingLogger(),
  });

  await expect(
    provisioning.allocateDirectoryWorkspaceForLaunch({
      cwd: path.join(tmpDir, "lookup-failure"),
    }),
  ).rejects.toBe(persistError);

  expect(errorLogs.map((entry) => entry.bindings.err)).toContain(persistError);
  expect(errorLogs.map((entry) => entry.bindings.lookupError)).toContain(lookupError);
});

test("existing workspace attach validates the record and never creates a fallback", async () => {
  const cwd = path.join(tmpDir, "existing");
  const created = await provisioning.createWorkspaceForDirectory(cwd);
  const resolved = await provisioning.requireExistingWorkspaceForLaunch(created.workspaceId);
  expect(resolved).toEqual(created);
  expect(await workspaceRegistry.list()).toHaveLength(1);

  await expect(provisioning.requireExistingWorkspaceForLaunch("wks_missing")).rejects.toMatchObject(
    {
      code: "unknown_workspace",
    } satisfies Partial<WorkspaceProvisioningError>,
  );
  expect(await workspaceRegistry.list()).toHaveLength(1);

  await workspaceRegistry.archive(created.workspaceId, "2026-01-01T00:00:00.000Z");
  await expect(
    provisioning.requireExistingWorkspaceForLaunch(created.workspaceId),
  ).rejects.toMatchObject({
    code: "archived_workspace",
  } satisfies Partial<WorkspaceProvisioningError>);
  expect(await workspaceRegistry.list()).toHaveLength(1);
});

test("retain from a second provisioning instance sharing the registry prevents archive", async () => {
  const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({
    cwd: path.join(tmpDir, "shared-registry"),
  });
  const sibling = createProvisioning({ inspect: async () => unusedInspection });
  sibling.retainDirectoryWorkspaceLaunch(launch.workspaceId);
  await launch.cleanupUnusedOnFailure();
  await expectActive(launch.workspaceId);
});

test("stale active selection after cleanup won follows the archived path instead of a stale descriptor", async () => {
  const cwd = path.join(tmpDir, "stale-select");
  const launch = await provisioning.allocateDirectoryWorkspaceForLaunch({ cwd });
  let listedStaleSnapshot = false;
  const racing = createProvisioning({
    workspaceRegistry: wrapRegistry({
      list: async () => {
        const snapshot = await workspaceRegistry.list();
        if (!listedStaleSnapshot) {
          listedStaleSnapshot = true;
          await launch.cleanupUnusedOnFailure();
        }
        return snapshot;
      },
    }),
    inspect: async () => unusedInspection,
  });

  const opened = await racing.findOrCreateWorkspaceForDirectory(cwd);

  expect(opened).toMatchObject({
    workspaceId: launch.workspaceId,
    archivedAt: null,
  });
  await expectActive(launch.workspaceId);
  expect(await workspaceRegistry.list()).toHaveLength(1);
});
