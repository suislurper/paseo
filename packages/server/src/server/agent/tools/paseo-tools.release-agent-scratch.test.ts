import { expect, test } from "vitest";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomUUID } from "node:crypto";

import { createTestLogger } from "../../../test-utils/test-logger.js";
import { createProviderSnapshotManagerStub } from "../../test-utils/session-stubs.js";
import { AgentManager } from "../agent-manager.js";
import { AgentStorage } from "../agent-storage.js";
import { AGENT_RUNTIME_MANIFEST_FILENAME, AgentRuntimeStorage } from "../agent-runtime-storage.js";
import type {
  AgentClient,
  AgentProvider,
  AgentSession,
  AgentSessionConfig,
} from "../agent-sdk-types.js";
import { createPaseoToolCatalog } from "./paseo-tools.js";

const logger = createTestLogger();

const TEST_CAPABILITIES = {
  supportsStreaming: false,
  supportsSessionPersistence: false,
  supportsSessionListing: true,
  supportsDynamicModes: false,
  supportsMcpServers: false,
  supportsReasoningStream: false,
  supportsToolInvocations: false,
} as const;

/** Minimal available client so AgentManager can be constructed with a provider map. */
class StubClient implements AgentClient {
  readonly capabilities = TEST_CAPABILITIES;

  constructor(public readonly provider: AgentProvider = "codex") {}

  async isAvailable(): Promise<boolean> {
    return true;
  }

  async fetchCatalog() {
    return {
      models: [
        {
          provider: this.provider,
          id: `${this.provider}-default`,
          label: `${this.provider} default`,
          isDefault: true,
        },
      ],
      modes: [],
    };
  }

  async createSession(config: AgentSessionConfig): Promise<AgentSession> {
    return {
      provider: this.provider,
      cwd: config.cwd,
    } as unknown as AgentSession;
  }
}

function createCatalogDeps(params: {
  agentManager: AgentManager;
  agentStorage: AgentStorage;
  callerAgentId?: string;
}) {
  return {
    agentManager: params.agentManager,
    agentStorage: params.agentStorage,
    providerSnapshotManager: createProviderSnapshotManagerStub() as never,
    logger,
    ...(params.callerAgentId !== undefined ? { callerAgentId: params.callerAgentId } : {}),
  };
}

function setupReleaseHarness(agentId: string) {
  const workdir = mkdtempSync(join(tmpdir(), "paseo-tool-release-"));
  const runtimeRoot = mkdtempSync(join(tmpdir(), "paseo-tool-release-rt-"));
  const storage = new AgentStorage(join(workdir, "agents"), logger);
  const agentRuntimeStorage = new AgentRuntimeStorage({ runtimeRoot });
  const manager = new AgentManager({
    clients: { codex: new StubClient() },
    registry: storage,
    agentRuntimeStorage,
    logger,
  });
  return { workdir, runtimeRoot, storage, agentRuntimeStorage, manager, agentId };
}

function cleanupHarness(workdir: string, runtimeRoot: string) {
  rmSync(workdir, { recursive: true, force: true });
  rmSync(runtimeRoot, { recursive: true, force: true });
}

test("release_agent_scratch self-release is idempotent and has no archive/delete side effects", async () => {
  const agentId = "00000000-0000-4000-8000-000000000b01";
  const { workdir, runtimeRoot, storage, agentRuntimeStorage, manager } =
    setupReleaseHarness(agentId);

  try {
    const prepared = await agentRuntimeStorage.prepare(agentId);
    const markerPath = join(prepared.scratchDir, "keep.tmp");
    writeFileSync(markerPath, "still-here");
    const artifactPath = join(prepared.artifactsDir, "kept.bin");
    writeFileSync(artifactPath, "artifact-bytes");

    const catalog = createPaseoToolCatalog(
      createCatalogDeps({ agentManager: manager, agentStorage: storage, callerAgentId: agentId }),
    );

    const first = await catalog.executeTool("release_agent_scratch", {
      agentId,
      generation: prepared.generation,
    });
    const firstContent = first.structuredContent as {
      agentId: string;
      generation: string;
      lifecycle: string;
      releasedAt: string;
    };
    expect(firstContent).toEqual({
      agentId,
      generation: prepared.generation,
      lifecycle: "released",
      releasedAt: firstContent.releasedAt,
    });
    expect(typeof firstContent.releasedAt).toBe("string");

    const second = await catalog.executeTool("release_agent_scratch", {
      agentId,
      generation: prepared.generation,
    });
    expect(second.structuredContent).toEqual(first.structuredContent);

    // Release is receipt-only: no agent archive, no file deletion.
    expect(manager.getAgent(agentId)).toBeNull();
    expect(readFileSync(markerPath, "utf8")).toBe("still-here");
    expect(readFileSync(artifactPath, "utf8")).toBe("artifact-bytes");
    expect(readFileSync(prepared.scratchManifestPath, "utf8")).toMatch(/"lifecycle": "released"/);
  } finally {
    cleanupHarness(workdir, runtimeRoot);
  }
});

test("release_agent_scratch rejects agent-scoped caller mismatch without changing storage", async () => {
  const agentId = "00000000-0000-4000-8000-000000000b02";
  const otherAgentId = "00000000-0000-4000-8000-000000000b03";
  const { workdir, runtimeRoot, storage, agentRuntimeStorage, manager } =
    setupReleaseHarness(agentId);

  try {
    const prepared = await agentRuntimeStorage.prepare(agentId);
    const catalog = createPaseoToolCatalog(
      createCatalogDeps({
        agentManager: manager,
        agentStorage: storage,
        callerAgentId: otherAgentId,
      }),
    );

    await expect(
      catalog.executeTool("release_agent_scratch", {
        agentId,
        generation: prepared.generation,
      }),
    ).rejects.toThrow(/may only release its own agent/);

    const manifest = JSON.parse(readFileSync(prepared.scratchManifestPath, "utf8")) as {
      lifecycle: string;
    };
    expect(manifest.lifecycle).toBe("active");
  } finally {
    cleanupHarness(workdir, runtimeRoot);
  }
});

test("release_agent_scratch rejects wrong generation without changing storage", async () => {
  const agentId = "00000000-0000-4000-8000-000000000b04";
  const { workdir, runtimeRoot, storage, agentRuntimeStorage, manager } =
    setupReleaseHarness(agentId);

  try {
    const prepared = await agentRuntimeStorage.prepare(agentId);
    const catalog = createPaseoToolCatalog(
      createCatalogDeps({ agentManager: manager, agentStorage: storage, callerAgentId: agentId }),
    );

    await expect(
      catalog.executeTool("release_agent_scratch", {
        agentId,
        generation: randomUUID(),
      }),
    ).rejects.toThrow(/generation mismatch/);

    const manifest = JSON.parse(readFileSync(prepared.scratchManifestPath, "utf8")) as {
      lifecycle: string;
      generation: string;
    };
    expect(manifest.lifecycle).toBe("active");
    expect(manifest.generation).toBe(prepared.generation);
  } finally {
    cleanupHarness(workdir, runtimeRoot);
  }
});

test("release_agent_scratch fails when runtime storage is unconfigured", async () => {
  const workdir = mkdtempSync(join(tmpdir(), "paseo-tool-release-unconfigured-"));
  const storage = new AgentStorage(join(workdir, "agents"), logger);
  const agentId = "00000000-0000-4000-8000-000000000b05";

  try {
    const manager = new AgentManager({
      clients: { codex: new StubClient() },
      registry: storage,
      logger,
    });

    // Global/operator session (no callerAgentId) — still fails when storage is unset.
    const catalog = createPaseoToolCatalog(
      createCatalogDeps({ agentManager: manager, agentStorage: storage }),
    );

    await expect(
      catalog.executeTool("release_agent_scratch", {
        agentId,
        generation: randomUUID(),
      }),
    ).rejects.toThrow(/Agent runtime storage is not configured/);
  } finally {
    rmSync(workdir, { recursive: true, force: true });
  }
});

test("global operator release_agent_scratch uses explicit agentId (no inferred owner)", async () => {
  const agentId = "00000000-0000-4000-8000-000000000b06";
  const { workdir, runtimeRoot, storage, agentRuntimeStorage, manager } =
    setupReleaseHarness(agentId);

  try {
    const prepared = await agentRuntimeStorage.prepare(agentId);

    // No callerAgentId: operator/global context releases the explicit agentId.
    const catalog = createPaseoToolCatalog(
      createCatalogDeps({ agentManager: manager, agentStorage: storage }),
    );

    const result = await catalog.executeTool("release_agent_scratch", {
      agentId,
      generation: prepared.generation,
    });
    expect(result.structuredContent).toMatchObject({
      agentId,
      generation: prepared.generation,
      lifecycle: "released",
    });
    expect(
      readFileSync(join(prepared.scratchDir, AGENT_RUNTIME_MANIFEST_FILENAME), "utf8"),
    ).toMatch(/"lifecycle": "released"/);
  } finally {
    cleanupHarness(workdir, runtimeRoot);
  }
});
