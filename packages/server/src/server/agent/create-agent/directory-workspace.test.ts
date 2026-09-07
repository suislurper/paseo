import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, test } from "vitest";

import { createTestLogger } from "../../../test-utils/test-logger.js";
import { createTestAgentClients } from "../../test-utils/fake-agent-client.js";
import { createNoopWorkspaceGitService } from "../../test-utils/workspace-git-service-stub.js";
import { createProviderSnapshotManagerStub } from "../../test-utils/session-stubs.js";
import { AgentManager } from "../agent-manager.js";
import { AgentStorage } from "../agent-storage.js";
import { createPaseoToolCatalog } from "../tools/paseo-tools.js";
import {
  FileBackedProjectRegistry,
  FileBackedWorkspaceRegistry,
  type WorkspaceRegistry,
} from "../../workspace-registry.js";
import {
  createWorkspaceProvisioningService,
  WorkspaceProvisioningError,
} from "../../session/workspace-provisioning/workspace-provisioning-service.js";
import { inspectDirectoryWorkspaceLaunchReferences } from "../../session/workspace-provisioning/directory-workspace-launch-references.js";
import { createAgentCommand, type CreateAgentFromMcpInput } from "./create.js";

const logger = createTestLogger();
const TIMESTAMP = "2026-09-07T00:00:00.000Z";

let workdir: string;

afterEach(() => {
  if (workdir) {
    rmSync(workdir, { recursive: true, force: true });
  }
});

function wrapRegistry(
  workspaceRegistry: FileBackedWorkspaceRegistry,
  overrides: Partial<WorkspaceRegistry>,
): WorkspaceRegistry {
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

async function createHarness(options?: {
  resolveCreateConfig?: () => Promise<never> | Promise<Record<string, never>>;
  workspaceRegistry?: (registry: FileBackedWorkspaceRegistry) => WorkspaceRegistry;
  onDirectoryWorkspaceCleanup?: (workspaceId: string) => void | Promise<void>;
}) {
  workdir = mkdtempSync(join(tmpdir(), "paseo-mcp-directory-launch-"));
  const cwd = join(workdir, "repo");
  mkdirSync(cwd, { recursive: true });
  const storage = new AgentStorage(join(workdir, "agents"), logger);
  const agentManager = new AgentManager({
    clients: createTestAgentClients(),
    registry: storage,
    logger,
  });
  const projectRegistry = new FileBackedProjectRegistry(join(workdir, "projects.json"), logger);
  const fileWorkspaceRegistry = new FileBackedWorkspaceRegistry(
    join(workdir, "workspaces.json"),
    logger,
  );
  await projectRegistry.initialize();
  await fileWorkspaceRegistry.initialize();
  const workspaceRegistry =
    options?.workspaceRegistry?.(fileWorkspaceRegistry) ?? fileWorkspaceRegistry;
  const snapshot = createProviderSnapshotManagerStub();
  if (options?.resolveCreateConfig) {
    snapshot.resolveCreateConfig.mockImplementation(options.resolveCreateConfig);
  }
  const provisioning = createWorkspaceProvisioningService({
    workspaceRegistry,
    projectRegistry,
    workspaceGitService: createNoopWorkspaceGitService(),
    logger,
    inspectDirectoryWorkspaceLaunchReferences: (workspaceId) =>
      inspectDirectoryWorkspaceLaunchReferences(workspaceId, {
        listManagedAgents: () => agentManager.listAgents(),
        listPersistedAgents: () => storage.list(),
        terminalManager: null,
      }),
  });
  const preexisting = await provisioning.createWorkspaceForDirectory(cwd, "Preexisting");
  const commandDeps = {
    agentManager,
    agentStorage: storage,
    logger,
    providerSnapshotManager: snapshot.manager,
    mcpLaunchProvisioning: provisioning,
    onDirectoryWorkspaceCleanup: options?.onDirectoryWorkspaceCleanup,
  };
  return {
    cwd,
    storage,
    agentManager,
    workspaceRegistry: fileWorkspaceRegistry,
    provisioning,
    preexisting,
    commandDeps,
    snapshot,
  };
}

function mcpDirectoryInput(
  cwd: string,
  overrides: Partial<CreateAgentFromMcpInput> = {},
): CreateAgentFromMcpInput {
  return {
    kind: "mcp",
    provider: "codex",
    title: "Directory agent",
    cwd,
    workspaceLaunch: { kind: "directory" },
    background: true,
    notifyOnFinish: false,
    ...overrides,
  };
}

async function expectActive(workspaceRegistry: FileBackedWorkspaceRegistry, workspaceId: string) {
  const record = await workspaceRegistry.get(workspaceId);
  expect(record).toMatchObject({ workspaceId, archivedAt: null });
}

function createCatalog(
  harness: Awaited<ReturnType<typeof createHarness>>,
  extras?: { callerAgentId?: string },
) {
  return createPaseoToolCatalog({
    agentManager: harness.agentManager,
    agentStorage: harness.storage,
    providerSnapshotManager: harness.snapshot.manager,
    logger,
    mcpLaunchProvisioning: harness.provisioning,
    callerAgentId: extras?.callerAgentId,
  });
}

test("invalid mode after MCP directory allocate archives only the owned unused workspace", async () => {
  const harness = await createHarness({
    resolveCreateConfig: async () => {
      throw new Error("Invalid mode 'nope' for provider 'codex'");
    },
  });

  await expect(
    createAgentCommand(harness.commandDeps, mcpDirectoryInput(harness.cwd, { mode: "nope" })),
  ).rejects.toThrow("Invalid mode 'nope'");

  const workspaces = await harness.workspaceRegistry.list();
  const active = workspaces.filter((workspace) => !workspace.archivedAt);
  const archived = workspaces.filter((workspace) => workspace.archivedAt);
  expect(active.map((workspace) => workspace.workspaceId)).toEqual([
    harness.preexisting.workspaceId,
  ]);
  expect(archived).toHaveLength(1);
  expect(archived[0]?.cwd).toBe(harness.cwd);
  expect(await harness.storage.list()).toEqual([]);
});

test("explicit directory create from a parent does not inherit the parent workspace", async () => {
  const harness = await createHarness();
  const parent = await harness.agentManager.createAgent(
    { provider: "codex", cwd: harness.cwd },
    undefined,
    { workspaceId: harness.preexisting.workspaceId },
  );

  const { snapshot } = await createAgentCommand(
    harness.commandDeps,
    mcpDirectoryInput(harness.cwd, {
      callerAgentId: parent.id,
      title: "Child directory",
      initialPrompt: "work in a new directory workspace",
    }),
  );

  const stored = await harness.storage.get(snapshot.id);
  expect(stored?.workspaceId).not.toBe(harness.preexisting.workspaceId);
  expect(stored?.workspaceId).toEqual(expect.any(String));
  await expectActive(harness.workspaceRegistry, stored!.workspaceId!);
  expect(await harness.workspaceRegistry.list()).toHaveLength(2);
});

test("invalid existing workspace id creates no agent and no workspace", async () => {
  const harness = await createHarness();
  const catalog = createCatalog(harness);

  await expect(
    catalog.executeTool("create_agent", {
      relationship: { kind: "detached" },
      workspace: { kind: "existing", workspaceId: "wks_missing" },
      title: "Missing",
      provider: "codex/gpt-5.4",
      initialPrompt: "Do work",
      background: true,
    }),
  ).rejects.toMatchObject({
    code: "unknown_workspace",
  } satisfies Partial<WorkspaceProvisioningError>);

  expect(await harness.workspaceRegistry.list()).toHaveLength(1);
  expect(await harness.storage.list()).toEqual([]);
  expect(harness.agentManager.listAgents()).toEqual([]);
});

test("direct parent fallback reuses the validated parent workspace", async () => {
  const harness = await createHarness();
  const parent = await harness.agentManager.createAgent(
    { provider: "codex", cwd: harness.cwd },
    undefined,
    { workspaceId: harness.preexisting.workspaceId },
  );

  const { snapshot } = await createAgentCommand(harness.commandDeps, {
    kind: "mcp",
    provider: "codex",
    title: "Child",
    cwd: harness.cwd,
    callerAgentId: parent.id,
    background: true,
    notifyOnFinish: false,
  });

  const stored = await harness.storage.get(snapshot.id);
  expect(stored?.workspaceId).toBe(harness.preexisting.workspaceId);
  expect(await harness.workspaceRegistry.list()).toHaveLength(1);
});

test("partial persisted registration preserves the owned directory workspace", async () => {
  const harness = await createHarness();
  harness.agentManager.createAgent = async (config, _agentId, options) => {
    await harness.storage.upsert({
      id: "partial-agent",
      provider: "codex",
      cwd: config.cwd,
      workspaceId: options.workspaceId,
      createdAt: TIMESTAMP,
      updatedAt: TIMESTAMP,
      labels: {},
      lastStatus: "initializing",
    });
    throw new Error("provider failed after register");
  };

  await expect(
    createAgentCommand(harness.commandDeps, mcpDirectoryInput(harness.cwd)),
  ).rejects.toThrow("provider failed after register");

  const stored = await harness.storage.get("partial-agent");
  expect(stored?.workspaceId).toEqual(expect.any(String));
  await expectActive(harness.workspaceRegistry, stored!.workspaceId!);
  const workspaces = await harness.workspaceRegistry.list();
  expect(workspaces.filter((workspace) => !workspace.archivedAt)).toHaveLength(2);
});

test("original create error survives a secondary cleanup update failure", async () => {
  const original = new Error("Invalid mode 'nope' for provider 'codex'");
  const harness = await createHarness({
    resolveCreateConfig: async () => {
      throw original;
    },
    onDirectoryWorkspaceCleanup: () => {
      throw new Error("workspace update failed");
    },
  });

  await expect(
    createAgentCommand(harness.commandDeps, mcpDirectoryInput(harness.cwd, { mode: "nope" })),
  ).rejects.toBe(original);
});

test("sticky retain during archive updater keeps an MCP existing attach, then archived attach rejects", async () => {
  let updateStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    updateStarted = resolve;
  });
  let releaseUpdate!: () => void;
  const updateGate = new Promise<void>((resolve) => {
    releaseUpdate = resolve;
  });

  const harness = await createHarness({
    resolveCreateConfig: async () => {
      throw new Error("Invalid mode 'nope' for provider 'codex'");
    },
    workspaceRegistry: (registry) =>
      wrapRegistry(registry, {
        update: async (workspaceId, updater) => {
          updateStarted();
          await updateGate;
          return registry.update(workspaceId, updater);
        },
      }),
  });

  const failedCreate = createAgentCommand(
    harness.commandDeps,
    mcpDirectoryInput(harness.cwd, { mode: "nope" }),
  );
  await started;
  const launched = (await harness.workspaceRegistry.list()).find(
    (workspace) => workspace.workspaceId !== harness.preexisting.workspaceId,
  );
  expect(launched?.archivedAt).toBeNull();

  const catalog = createPaseoToolCatalog({
    agentManager: harness.agentManager,
    agentStorage: harness.storage,
    providerSnapshotManager: createProviderSnapshotManagerStub().manager,
    logger,
    mcpLaunchProvisioning: harness.provisioning,
  });
  const attached = await catalog.executeTool("create_agent", {
    relationship: { kind: "detached" },
    workspace: { kind: "existing", workspaceId: launched!.workspaceId },
    title: "Attach during cleanup",
    provider: "codex/gpt-5.4",
    initialPrompt: "Keep this workspace",
    background: true,
  });
  releaseUpdate();
  await expect(failedCreate).rejects.toThrow("Invalid mode 'nope'");
  await expectActive(harness.workspaceRegistry, launched!.workspaceId);

  const attachedId = (attached.structuredContent as { agentId: string }).agentId;
  expect((await harness.storage.get(attachedId))?.workspaceId).toBe(launched!.workspaceId);

  await harness.workspaceRegistry.archive(launched!.workspaceId, TIMESTAMP);
  await expect(
    catalog.executeTool("create_agent", {
      relationship: { kind: "detached" },
      workspace: { kind: "existing", workspaceId: launched!.workspaceId },
      title: "Attach after archive",
      provider: "codex/gpt-5.4",
      initialPrompt: "Should fail",
      background: true,
    }),
  ).rejects.toMatchObject({
    code: "archived_workspace",
  } satisfies Partial<WorkspaceProvisioningError>);
});
