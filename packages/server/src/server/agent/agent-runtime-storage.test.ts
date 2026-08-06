import { randomUUID } from "node:crypto";
import {
  chmodSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, test } from "vitest";

import {
  AGENT_RUNTIME_ARTIFACTS_DIRNAME,
  AGENT_RUNTIME_MANIFEST_FILENAME,
  AGENT_RUNTIME_SCRATCH_DIRNAME,
  AgentRuntimeStorage,
} from "./agent-runtime-storage.js";
import { PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE } from "../private-files.js";

const MODE_MASK = 0o777;
const tempRoots: string[] = [];

function createTempRuntimeRoot(): string {
  const root = mkdtempSync(path.join(tmpdir(), "paseo-agent-runtime-"));
  tempRoots.push(root);
  return root;
}

function modeOf(filePath: string): number {
  return statSync(filePath).mode & MODE_MASK;
}

function readJson(filePath: string): unknown {
  return JSON.parse(readFileSync(filePath, "utf8"));
}

afterEach(() => {
  while (tempRoots.length > 0) {
    const root = tempRoots.pop();
    if (root) {
      rmSync(root, { recursive: true, force: true });
    }
  }
});

describe("AgentRuntimeStorage", () => {
  test("rejects a non-absolute runtime root", () => {
    expect(() => new AgentRuntimeStorage({ runtimeRoot: "relative/runtime" })).toThrow(
      /absolute filesystem path/,
    );
  });

  test("rejects an invalid agent id", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const storage = new AgentRuntimeStorage({ runtimeRoot });

    await expect(storage.prepare("not-a-uuid")).rejects.toThrow(/agentId must be a UUID/);
    await expect(storage.prepare("../escape")).rejects.toThrow(/agentId must be a UUID/);
    await expect(storage.prepare(path.join(runtimeRoot, "escape"))).rejects.toThrow(
      /agentId must be a UUID/,
    );
  });

  test("creates scratch and artifacts directories with private modes", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const storage = new AgentRuntimeStorage({ runtimeRoot });
    const agentId = randomUUID();

    const prepared = await storage.prepare(agentId);

    expect(prepared.agentId).toBe(agentId);
    expect(prepared.lifecycle).toBe("active");
    expect(prepared.scratchDir).toBe(
      path.join(runtimeRoot, AGENT_RUNTIME_SCRATCH_DIRNAME, agentId),
    );
    expect(prepared.artifactsDir).toBe(
      path.join(runtimeRoot, AGENT_RUNTIME_ARTIFACTS_DIRNAME, agentId),
    );
    expect(statSync(prepared.scratchDir).isDirectory()).toBe(true);
    expect(statSync(prepared.artifactsDir).isDirectory()).toBe(true);

    if (process.platform !== "win32") {
      expect(modeOf(prepared.scratchDir)).toBe(PRIVATE_DIRECTORY_MODE);
      expect(modeOf(prepared.artifactsDir)).toBe(PRIVATE_DIRECTORY_MODE);
      expect(modeOf(path.join(runtimeRoot, AGENT_RUNTIME_SCRATCH_DIRNAME))).toBe(
        PRIVATE_DIRECTORY_MODE,
      );
      expect(modeOf(path.join(runtimeRoot, AGENT_RUNTIME_ARTIFACTS_DIRNAME))).toBe(
        PRIVATE_DIRECTORY_MODE,
      );
      expect(modeOf(prepared.scratchManifestPath)).toBe(PRIVATE_FILE_MODE);
      expect(modeOf(prepared.artifactsManifestPath)).toBe(PRIVATE_FILE_MODE);
    }
  });

  test("writes a daemon-owned scratch manifest and retained artifacts marker", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const storage = new AgentRuntimeStorage({ runtimeRoot });
    const agentId = randomUUID();

    const prepared = await storage.prepare(agentId);
    const scratchManifest = readJson(prepared.scratchManifestPath) as Record<string, unknown>;
    const artifactsManifest = readJson(prepared.artifactsManifestPath) as Record<string, unknown>;

    expect(scratchManifest).toMatchObject({
      schemaVersion: 1,
      agentId,
      generation: prepared.generation,
      lifecycle: "active",
    });
    expect(typeof scratchManifest.createdAt).toBe("string");
    expect(scratchManifest).not.toHaveProperty("secret");
    expect(scratchManifest).not.toHaveProperty("password");
    expect(scratchManifest).not.toHaveProperty("token");

    expect(artifactsManifest).toEqual({
      schemaVersion: 1,
      agentId,
      retention: "retained",
      createdAt: expect.any(String),
    });
  });

  test("reuses a valid matching generation across prepare calls (daemon restart)", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const agentId = randomUUID();

    const first = new AgentRuntimeStorage({ runtimeRoot });
    const preparedFirst = await first.prepare(agentId);

    const second = new AgentRuntimeStorage({ runtimeRoot });
    const preparedSecond = await second.prepare(agentId);

    expect(preparedSecond.generation).toBe(preparedFirst.generation);
    expect(preparedSecond.lifecycle).toBe("active");
    expect(preparedSecond.scratchDir).toBe(preparedFirst.scratchDir);
    expect(preparedSecond.artifactsDir).toBe(preparedFirst.artifactsDir);
  });

  test("rejects a symlink runtime root", async () => {
    const parent = createTempRuntimeRoot();
    const realRoot = path.join(parent, "real-root");
    const linkRoot = path.join(parent, "link-root");
    mkdirSync(realRoot, { recursive: true });
    symlinkSync(realRoot, linkRoot, process.platform === "win32" ? "junction" : "dir");

    // Junctions are not always reported as symlinks on Windows; cover POSIX fail-closed.
    if (process.platform === "win32" && !lstatSync(linkRoot).isSymbolicLink()) {
      return;
    }

    const storage = new AgentRuntimeStorage({ runtimeRoot: linkRoot });
    await expect(storage.prepare(randomUUID())).rejects.toThrow(/symlink/);
  });

  test("rejects a symlink path component under the runtime root", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const agentId = randomUUID();
    const outside = path.join(runtimeRoot, "outside-target");
    const scratchParent = path.join(runtimeRoot, AGENT_RUNTIME_SCRATCH_DIRNAME);
    mkdirSync(outside, { recursive: true });
    mkdirSync(scratchParent, { recursive: true });
    symlinkSync(
      outside,
      path.join(scratchParent, agentId),
      process.platform === "win32" ? "junction" : "dir",
    );

    if (
      process.platform === "win32" &&
      !lstatSync(path.join(scratchParent, agentId)).isSymbolicLink()
    ) {
      return;
    }

    const storage = new AgentRuntimeStorage({ runtimeRoot });
    await expect(storage.prepare(agentId)).rejects.toThrow(/symlink/);
  });

  test("rejects a non-directory collision at the agent scratch path", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const agentId = randomUUID();
    const scratchParent = path.join(runtimeRoot, AGENT_RUNTIME_SCRATCH_DIRNAME);
    mkdirSync(scratchParent, { recursive: true });
    writeFileSync(path.join(scratchParent, agentId), "not-a-directory");

    const storage = new AgentRuntimeStorage({ runtimeRoot });
    await expect(storage.prepare(agentId)).rejects.toThrow(/not a directory/);
  });

  test("rejects a malformed scratch manifest", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const agentId = randomUUID();
    const scratchDir = path.join(runtimeRoot, AGENT_RUNTIME_SCRATCH_DIRNAME, agentId);
    mkdirSync(scratchDir, { recursive: true });
    writeFileSync(path.join(scratchDir, AGENT_RUNTIME_MANIFEST_FILENAME), "{not-json");

    const storage = new AgentRuntimeStorage({ runtimeRoot });
    await expect(storage.prepare(agentId)).rejects.toThrow(/malformed/);
  });

  test("rejects a scratch manifest with a mismatched agent id", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const agentId = randomUUID();
    const otherAgentId = randomUUID();
    const scratchDir = path.join(runtimeRoot, AGENT_RUNTIME_SCRATCH_DIRNAME, agentId);
    mkdirSync(scratchDir, { recursive: true });
    writeFileSync(
      path.join(scratchDir, AGENT_RUNTIME_MANIFEST_FILENAME),
      `${JSON.stringify(
        {
          schemaVersion: 1,
          agentId: otherAgentId,
          generation: randomUUID(),
          createdAt: new Date().toISOString(),
          lifecycle: "active",
        },
        null,
        2,
      )}\n`,
    );

    const storage = new AgentRuntimeStorage({ runtimeRoot });
    await expect(storage.prepare(agentId)).rejects.toThrow(/agentId mismatch/);
  });

  test("markReleased requires exact generation and is idempotent", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const storage = new AgentRuntimeStorage({ runtimeRoot });
    const agentId = randomUUID();
    const prepared = await storage.prepare(agentId);

    const markerPath = path.join(prepared.artifactsDir, "user-artifact.txt");
    writeFileSync(markerPath, "keep-me");
    const artifactsManifestBefore = readFileSync(prepared.artifactsManifestPath, "utf8");
    const artifactBefore = readFileSync(markerPath, "utf8");
    const artifactsMtimeBefore = statSync(prepared.artifactsManifestPath).mtimeMs;

    await expect(storage.markReleased({ agentId, generation: randomUUID() })).rejects.toThrow(
      /generation mismatch/,
    );

    const released = await storage.markReleased({
      agentId,
      generation: prepared.generation,
    });
    expect(released.lifecycle).toBe("released");

    const scratchAfterFirst = readJson(prepared.scratchManifestPath) as {
      lifecycle: string;
      releasedAt?: string;
      generation: string;
    };
    expect(scratchAfterFirst.lifecycle).toBe("released");
    expect(typeof scratchAfterFirst.releasedAt).toBe("string");
    const firstReleasedAt = scratchAfterFirst.releasedAt;

    const releasedAgain = await storage.markReleased({
      agentId,
      generation: prepared.generation,
    });
    expect(releasedAgain.lifecycle).toBe("released");

    const scratchAfterSecond = readJson(prepared.scratchManifestPath) as {
      lifecycle: string;
      releasedAt?: string;
      generation: string;
    };
    expect(scratchAfterSecond.lifecycle).toBe("released");
    expect(scratchAfterSecond.releasedAt).toBe(firstReleasedAt);
    expect(scratchAfterSecond.generation).toBe(prepared.generation);

    // Artifacts must be untouched by release.
    expect(readFileSync(prepared.artifactsManifestPath, "utf8")).toBe(artifactsManifestBefore);
    expect(readFileSync(markerPath, "utf8")).toBe(artifactBefore);
    expect(statSync(prepared.artifactsManifestPath).mtimeMs).toBe(artifactsMtimeBefore);
    expect(statSync(prepared.artifactsDir).isDirectory()).toBe(true);
  });

  test("markReleased fails when the agent id does not match the scratch directory manifest", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const storage = new AgentRuntimeStorage({ runtimeRoot });
    const agentId = randomUUID();
    const prepared = await storage.prepare(agentId);

    // Corrupt the on-disk agentId while keeping the path directory name.
    writeFileSync(
      prepared.scratchManifestPath,
      `${JSON.stringify(
        {
          schemaVersion: 1,
          agentId: randomUUID(),
          generation: prepared.generation,
          createdAt: new Date().toISOString(),
          lifecycle: "active",
        },
        null,
        2,
      )}\n`,
    );

    await expect(
      storage.markReleased({ agentId, generation: prepared.generation }),
    ).rejects.toThrow(/agentId mismatch/);
  });

  test("prepare still succeeds after release without deleting artifacts", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const storage = new AgentRuntimeStorage({ runtimeRoot });
    const agentId = randomUUID();
    const prepared = await storage.prepare(agentId);
    await storage.markReleased({ agentId, generation: prepared.generation });

    const markerPath = path.join(prepared.artifactsDir, "retained.bin");
    writeFileSync(markerPath, "blob");

    const again = await storage.prepare(agentId);
    expect(again.generation).toBe(prepared.generation);
    expect(again.lifecycle).toBe("released");
    expect(readFileSync(markerPath, "utf8")).toBe("blob");
  });

  test("does not delete existing content on prepare reuse", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const storage = new AgentRuntimeStorage({ runtimeRoot });
    const agentId = randomUUID();
    const prepared = await storage.prepare(agentId);

    const scratchFile = path.join(prepared.scratchDir, "work.txt");
    const artifactFile = path.join(prepared.artifactsDir, "out.bin");
    writeFileSync(scratchFile, "scratch-data");
    writeFileSync(artifactFile, "artifact-data");

    await storage.prepare(agentId);

    expect(readFileSync(scratchFile, "utf8")).toBe("scratch-data");
    expect(readFileSync(artifactFile, "utf8")).toBe("artifact-data");
  });
});

describe.skipIf(process.platform === "win32")("AgentRuntimeStorage permissions", () => {
  test("restores 0700 on reused agent directories", async () => {
    const runtimeRoot = createTempRuntimeRoot();
    const storage = new AgentRuntimeStorage({ runtimeRoot });
    const agentId = randomUUID();
    const prepared = await storage.prepare(agentId);

    chmodSync(prepared.scratchDir, 0o755);
    chmodSync(prepared.artifactsDir, 0o755);

    await storage.prepare(agentId);

    expect(modeOf(prepared.scratchDir)).toBe(PRIVATE_DIRECTORY_MODE);
    expect(modeOf(prepared.artifactsDir)).toBe(PRIVATE_DIRECTORY_MODE);
  });
});
