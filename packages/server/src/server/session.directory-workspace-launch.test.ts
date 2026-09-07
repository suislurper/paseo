import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, expect, test, vi } from "vitest";

import type { SessionOutboundMessage } from "@getpaseo/protocol/messages";

import { Session } from "./session.js";
import { AgentManager } from "./agent/agent-manager.js";
import { AgentStorage } from "./agent/agent-storage.js";
import { WorkspaceAutoName } from "./workspace-auto-name.js";
import {
  FileBackedProjectRegistry,
  FileBackedWorkspaceRegistry,
  createPersistedProjectRecord,
  createPersistedWorkspaceRecord,
} from "./workspace-registry.js";
import { createNoopWorkspaceGitService } from "./test-utils/workspace-git-service-stub.js";
import { createTestAgentClients } from "./test-utils/fake-agent-client.js";
import {
  asSessionLogger,
  asDownloadTokenStore,
  asPushTokenStore,
  asChatService,
  asScheduleService,
  asLoopService,
  asCheckoutDiffManager,
  asDaemonConfigStore,
  createProviderSnapshotManagerStub,
} from "./test-utils/session-stubs.js";

const TIMESTAMP = "2026-05-07T00:00:00.000Z";

let workdir: string;

afterEach(() => {
  if (workdir) {
    rmSync(workdir, { recursive: true, force: true });
  }
});

function createLogger() {
  const logger = {
    child: () => logger,
    trace: vi.fn(),
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  };
  return logger;
}

function findCreateFailed(emitted: SessionOutboundMessage[]): SessionOutboundMessage | undefined {
  return emitted.find(
    (message) => message.type === "status" && message.payload.status === "agent_create_failed",
  );
}

async function createDirectoryLaunchSession(options?: {
  resolveCreateConfig?: () => Promise<never>;
  seedSameCwdWorkspace?: boolean;
}) {
  workdir = mkdtempSync(path.join(tmpdir(), "paseo-directory-launch-"));
  const cwd = path.join(workdir, "repo");
  mkdirSync(cwd, { recursive: true });
  const keptFile = path.join(cwd, "keep.txt");
  writeFileSync(keptFile, "keep me");

  const logger = createLogger();
  const agentStorage = new AgentStorage(path.join(workdir, "agents"), asSessionLogger(logger));
  const clients = createTestAgentClients();
  const agentManager = new AgentManager({
    clients: { codex: clients.codex },
    registry: agentStorage,
    logger: asSessionLogger(logger),
  });
  const projectRegistry = new FileBackedProjectRegistry(
    path.join(workdir, "projects.json"),
    asSessionLogger(logger),
  );
  const workspaceRegistry = new FileBackedWorkspaceRegistry(
    path.join(workdir, "workspaces.json"),
    asSessionLogger(logger),
  );
  await projectRegistry.initialize();
  await workspaceRegistry.initialize();
  await projectRegistry.upsert(
    createPersistedProjectRecord({
      projectId: "proj-existing",
      rootPath: cwd,
      kind: "non_git",
      displayName: "repo",
      createdAt: TIMESTAMP,
      updatedAt: TIMESTAMP,
    }),
  );
  if (options?.seedSameCwdWorkspace !== false) {
    await workspaceRegistry.upsert(
      createPersistedWorkspaceRecord({
        workspaceId: "ws-preexisting",
        projectId: "proj-existing",
        cwd,
        kind: "directory",
        displayName: "repo",
        createdAt: TIMESTAMP,
        updatedAt: TIMESTAMP,
      }),
    );
  }

  const snapshot = createProviderSnapshotManagerStub();
  if (options?.resolveCreateConfig) {
    snapshot.resolveCreateConfig.mockImplementation(options.resolveCreateConfig);
  }
  const workspaceGitService = createNoopWorkspaceGitService();
  const emitted: SessionOutboundMessage[] = [];
  const session = new Session({
    clientId: "test-client",
    scopes: ["*"],
    appVersion: null,
    onMessage: (message) => emitted.push(message),
    logger: asSessionLogger(logger),
    downloadTokenStore: asDownloadTokenStore(),
    pushTokenStore: asPushTokenStore(),
    paseoHome: path.join(workdir, "paseo-home"),
    agentManager,
    agentStorage,
    projectRegistry,
    workspaceRegistry,
    chatService: asChatService(),
    scheduleService: asScheduleService(),
    loopService: asLoopService(),
    checkoutDiffManager: asCheckoutDiffManager({
      subscribe: async () => ({
        initial: { cwd, files: [], error: null },
        unsubscribe: () => {},
      }),
      scheduleRefreshForCwd: () => {},
      onWorkspaceStateMayHaveChanged: () => {},
      invalidateForge: () => {},
      getMetrics: () => ({
        checkoutDiffTargetCount: 0,
        checkoutDiffSubscriptionCount: 0,
        checkoutDiffWatcherCount: 0,
        checkoutDiffFallbackRefreshTargetCount: 0,
      }),
      dispose: () => {},
    }),
    workspaceGitService,
    workspaceAutoName: new WorkspaceAutoName({
      agentManager,
      workspaceRegistry,
      workspaceGitService,
      providerSnapshotManager: snapshot.manager,
      readDaemonConfig: () => ({ metadataGeneration: { providers: [] } }),
      gitMutation: { notifyGitMutation: async () => {} },
      emitWorkspaceUpdateForCwd: async () => {},
      emitWorkspaceUpdateForWorkspaceId: async () => {},
      logger: asSessionLogger(logger),
    }),
    daemonConfigStore: asDaemonConfigStore({
      get: () => ({ mcp: { injectIntoAgents: false }, providers: {} }),
      onChange: () => () => {},
    }),
    mcpBaseUrl: null,
    stt: null,
    tts: null,
    providerSnapshotManager: snapshot.manager,
    terminalManager: null,
  });

  return {
    session,
    emitted,
    cwd,
    keptFile,
    workspaceRegistry,
    agentManager,
    agentStorage,
  };
}

test("invalid mode after directory allocation archives only the new workspace and leaves files", async () => {
  const harness = await createDirectoryLaunchSession({
    resolveCreateConfig: async () => {
      throw new Error("Invalid mode 'nope' for provider 'codex'");
    },
  });

  await harness.session.handleMessage({
    type: "create_agent_request",
    requestId: "req-invalid-mode",
    config: { provider: "codex", cwd: harness.cwd, modeId: "nope" },
    attachments: [],
    labels: {},
  });

  const workspaces = await harness.workspaceRegistry.list();
  const active = workspaces.filter((workspace) => !workspace.archivedAt);
  const archived = workspaces.filter((workspace) => workspace.archivedAt);
  expect(active.map((workspace) => workspace.workspaceId)).toEqual(["ws-preexisting"]);
  expect(archived).toHaveLength(1);
  expect(archived[0]?.cwd).toBe(harness.cwd);
  expect(archived[0]?.workspaceId).not.toBe("ws-preexisting");
  expect(readFileSync(harness.keptFile, "utf8")).toBe("keep me");
  expect(harness.agentManager.listAgents()).toEqual([]);
  expect(findCreateFailed(harness.emitted)).toMatchObject({
    payload: { status: "agent_create_failed", requestId: "req-invalid-mode" },
  });
});

test("invalid explicit workspace id creates no workspace or agent", async () => {
  const harness = await createDirectoryLaunchSession();

  await harness.session.handleMessage({
    type: "create_agent_request",
    requestId: "req-invalid-explicit",
    workspaceId: "wks_missing",
    config: { provider: "codex", cwd: harness.cwd },
    attachments: [],
    labels: {},
  });

  expect(await harness.workspaceRegistry.list()).toEqual([
    expect.objectContaining({ workspaceId: "ws-preexisting", archivedAt: null }),
  ]);
  expect(harness.agentManager.listAgents()).toEqual([]);
  expect(findCreateFailed(harness.emitted)).toMatchObject({
    payload: { status: "agent_create_failed", requestId: "req-invalid-explicit" },
  });
});

test("open_project during a failed directory launch keeps the independently opened workspace active", async () => {
  let releaseLaunch!: () => void;
  const launchGate = new Promise<void>((resolve) => {
    releaseLaunch = resolve;
  });
  let launchReached!: () => void;
  const launchStarted = new Promise<void>((resolve) => {
    launchReached = resolve;
  });
  const harness = await createDirectoryLaunchSession({
    seedSameCwdWorkspace: false,
    resolveCreateConfig: async () => {
      launchReached();
      await launchGate;
      throw new Error("Invalid mode 'nope' for provider 'codex'");
    },
  });

  const createPromise = harness.session.handleMessage({
    type: "create_agent_request",
    requestId: "req-open-during-launch",
    config: { provider: "codex", cwd: harness.cwd, modeId: "nope" },
    attachments: [],
    labels: {},
  });
  await launchStarted;

  await harness.session.handleMessage({
    type: "open_project_request",
    cwd: harness.cwd,
    requestId: "req-open-project",
  });

  const openResponse = harness.emitted.find(
    (message) =>
      message.type === "open_project_response" && message.payload.requestId === "req-open-project",
  );
  expect(openResponse).toMatchObject({
    type: "open_project_response",
    payload: { requestId: "req-open-project", error: null },
  });
  const openedId =
    openResponse?.type === "open_project_response" ? openResponse.payload.workspace?.id : undefined;
  expect(openedId).toEqual(expect.any(String));

  releaseLaunch();
  await createPromise;

  expect(await harness.workspaceRegistry.get(openedId as string)).toMatchObject({
    workspaceId: openedId,
    archivedAt: null,
  });
  expect(readFileSync(harness.keptFile, "utf8")).toBe("keep me");
  expect(harness.agentManager.listAgents()).toEqual([]);
  expect(findCreateFailed(harness.emitted)).toMatchObject({
    payload: { status: "agent_create_failed", requestId: "req-open-during-launch" },
  });
});

test("partial registered agent keeps the allocated directory workspace", async () => {
  const harness = await createDirectoryLaunchSession();
  const originalCreateAgent = harness.agentManager.createAgent.bind(harness.agentManager);
  harness.agentManager.createAgent = async (config, agentId, options) => {
    await originalCreateAgent(config, agentId, options);
    throw new Error("failed after registration");
  };

  await harness.session.handleMessage({
    type: "create_agent_request",
    requestId: "req-partial-register",
    config: { provider: "codex", cwd: harness.cwd },
    attachments: [],
    labels: {},
  });

  const agents = harness.agentManager.listAgents();
  expect(agents).toHaveLength(1);
  const workspaceId = agents[0]?.workspaceId;
  expect(workspaceId).toEqual(expect.any(String));
  expect(await harness.workspaceRegistry.get(workspaceId as string)).toMatchObject({
    workspaceId,
    archivedAt: null,
  });
  const persisted = await harness.agentStorage.list();
  expect(persisted.some((record) => record.workspaceId === workspaceId)).toBe(true);
  expect(findCreateFailed(harness.emitted)).toMatchObject({
    payload: { status: "agent_create_failed", requestId: "req-partial-register" },
  });
});
