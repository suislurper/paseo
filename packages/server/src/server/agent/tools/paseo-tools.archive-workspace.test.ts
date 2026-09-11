import { expect, test } from "vitest";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { createTestLogger } from "../../../test-utils/test-logger.js";
import { createProviderSnapshotManagerStub } from "../../test-utils/session-stubs.js";
import {
  FileBackedWorkspaceRegistry,
  createPersistedWorkspaceRecord,
} from "../../workspace-registry.js";
import { AgentManager } from "../agent-manager.js";
import { AgentStorage } from "../agent-storage.js";
import { createPaseoToolCatalog, type PaseoToolHostDependencies } from "./paseo-tools.js";

test("archive_workspace archives an exact external workspace and retains its files on retry", async () => {
  const root = mkdtempSync(join(tmpdir(), "paseo-mcp-archive-"));
  const logger = createTestLogger();
  try {
    const marker = join(root, "keep.txt");
    writeFileSync(marker, "keep");
    const registry = new FileBackedWorkspaceRegistry(join(root, "workspaces.json"), logger);
    await registry.initialize();
    const record = createPersistedWorkspaceRecord({
      workspaceId: "wks_exact",
      projectId: "project",
      cwd: root,
      kind: "directory",
      displayName: "external",
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    });
    await registry.upsert(record);
    const storage = new AgentStorage(join(root, "agents"), logger);
    const manager = new AgentManager({ clients: {}, registry: storage, logger });
    const catalog = createPaseoToolCatalog({
      agentManager: manager,
      agentStorage: storage,
      logger,
      providerSnapshotManager: createProviderSnapshotManagerStub() as never,
      workspaceRegistry: registry,
      github: {} as PaseoToolHostDependencies["github"],
      workspaceGitService: {} as PaseoToolHostDependencies["workspaceGitService"],
      findWorkspaceIdForCwd: async () => record.workspaceId,
      listActiveWorkspaces: async () =>
        (await registry.list()).filter((entry) => !entry.archivedAt),
      archiveWorkspaceRecord: async (workspaceId) => {
        await registry.update(workspaceId, (current) => ({
          ...current,
          archivedAt: new Date().toISOString(),
        }));
      },
      emitWorkspaceUpdatesForWorkspaceIds: async () => {},
      markWorkspaceArchiving: () => {},
      clearWorkspaceArchiving: () => {},
    });
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const result = await catalog.executeTool("archive_workspace", {
        workspaceId: record.workspaceId,
        mode: "archive_only",
      });
      expect(result.structuredContent).toMatchObject({
        workspaceId: record.workspaceId,
        cleanup: { status: "retained" },
      });
      expect((await registry.get(record.workspaceId))?.archivedAt).toBeTruthy();
      expect(existsSync(marker)).toBe(true);
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
