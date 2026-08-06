import { randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import path from "node:path";
import { z } from "zod";

import { writeFileAtomic } from "../atomic-file.js";
import { PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE } from "../private-files.js";

const AgentIdSchema = z.guid();
const GenerationSchema = z.string().uuid();

export const AGENT_RUNTIME_MANIFEST_SCHEMA_VERSION = 1 as const;
export const AGENT_RUNTIME_SCRATCH_DIRNAME = "scratch";
export const AGENT_RUNTIME_ARTIFACTS_DIRNAME = "artifacts";
export const AGENT_RUNTIME_MANIFEST_FILENAME = "manifest.json";

export type AgentRuntimeLifecycleState = "active" | "released";

const ScratchManifestSchema = z
  .object({
    schemaVersion: z.literal(AGENT_RUNTIME_MANIFEST_SCHEMA_VERSION),
    agentId: AgentIdSchema,
    generation: GenerationSchema,
    createdAt: z.string().min(1),
    lifecycle: z.enum(["active", "released"]),
    releasedAt: z.string().min(1).optional(),
  })
  .strict();

const ArtifactsManifestSchema = z
  .object({
    schemaVersion: z.literal(AGENT_RUNTIME_MANIFEST_SCHEMA_VERSION),
    agentId: AgentIdSchema,
    // Retained classification marker — release never deletes or rewrites this file.
    retention: z.literal("retained"),
    createdAt: z.string().min(1),
  })
  .strict();

export type AgentScratchManifest = z.infer<typeof ScratchManifestSchema>;
export type AgentArtifactsManifest = z.infer<typeof ArtifactsManifestSchema>;

export interface AgentRuntimeLaunchPaths {
  agentId: string;
  generation: string;
  scratchDir: string;
  artifactsDir: string;
  scratchManifestPath: string;
  artifactsManifestPath: string;
  lifecycle: AgentRuntimeLifecycleState;
}

/** Exact-generation scratch release receipt. Never implies deletion or archive. */
export interface AgentScratchReleaseReceipt {
  agentId: string;
  generation: string;
  lifecycle: "released";
  releasedAt: string;
}

export interface AgentRuntimeStorageOptions {
  runtimeRoot: string;
}

export class AgentRuntimeStorageError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options?.cause !== undefined ? { cause: options.cause } : undefined);
    this.name = "AgentRuntimeStorageError";
  }
}

function isAbsoluteFilesystemPath(value: string): boolean {
  // Native platform absolute only — a Windows drive path must not be accepted on POSIX
  // (path.resolve would treat it as relative and mis-place the runtime root).
  return path.isAbsolute(value);
}

function isMissingEntryError(error: unknown): boolean {
  return (error as NodeJS.ErrnoException | null)?.code === "ENOENT";
}

function validateAgentId(agentId: string, source: string): string {
  const result = AgentIdSchema.safeParse(agentId);
  if (!result.success) {
    throw new AgentRuntimeStorageError(`${source}: agentId must be a UUID`);
  }
  return result.data;
}

function validateGeneration(generation: string, source: string): string {
  const result = GenerationSchema.safeParse(generation);
  if (!result.success) {
    throw new AgentRuntimeStorageError(`${source}: generation must be a UUID`);
  }
  return result.data;
}

/**
 * Durable per-agent runtime storage under a configured absolute runtime root.
 *
 * Layout:
 *   {runtimeRoot}/scratch/{agentId}/manifest.json
 *   {runtimeRoot}/artifacts/{agentId}/manifest.json
 *
 * Scratch is generation-scoped launch state. Artifacts are retained and are never
 * touched by release. This primitive does not delete files or directories.
 */
export class AgentRuntimeStorage {
  private readonly runtimeRoot: string;

  constructor(options: AgentRuntimeStorageOptions) {
    const trimmed = options.runtimeRoot.trim();
    if (!trimmed) {
      throw new AgentRuntimeStorageError("runtimeRoot must be a non-empty absolute path");
    }
    if (!isAbsoluteFilesystemPath(trimmed)) {
      throw new AgentRuntimeStorageError("runtimeRoot must be an absolute filesystem path");
    }
    this.runtimeRoot = path.resolve(trimmed);
  }

  getRuntimeRoot(): string {
    return this.runtimeRoot;
  }

  /**
   * Ensure scratch + artifacts directories exist for the agent, create or reuse the
   * daemon-owned scratch generation manifest, and ensure the retained artifacts marker.
   *
   * - Active generation: reused across daemon restarts (idempotent prepare).
   * - Released generation: atomically rotated to a fresh active generation. Bytes under
   *   scratch/artifacts are retained; only the scratch manifest generation + lifecycle
   *   change. This prevents a stale release receipt from authorizing cleanup after relaunch.
   */
  async prepare(agentId: string): Promise<AgentRuntimeLaunchPaths> {
    const validatedAgentId = validateAgentId(agentId, "prepare");
    const paths = this.derivePaths(validatedAgentId);

    await this.assertSafeRuntimeRoot();
    await this.ensurePrivateDirectory(path.join(this.runtimeRoot, AGENT_RUNTIME_SCRATCH_DIRNAME));
    await this.ensurePrivateDirectory(path.join(this.runtimeRoot, AGENT_RUNTIME_ARTIFACTS_DIRNAME));
    await this.ensureAgentDirectory(paths.scratchDir);
    await this.ensureAgentDirectory(paths.artifactsDir);
    await this.assertNoSymlinkAlongPath(paths.scratchDir);
    await this.assertNoSymlinkAlongPath(paths.artifactsDir);

    const scratchManifest = await this.loadOrCreateScratchManifest(paths, validatedAgentId);
    await this.ensureArtifactsManifest(paths, validatedAgentId);

    return {
      agentId: validatedAgentId,
      generation: scratchManifest.generation,
      scratchDir: paths.scratchDir,
      artifactsDir: paths.artifactsDir,
      scratchManifestPath: paths.scratchManifestPath,
      artifactsManifestPath: paths.artifactsManifestPath,
      lifecycle: scratchManifest.lifecycle,
    };
  }

  /**
   * Mark a scratch generation released. Requires exact agent ID + generation.
   * Repeated exact release is idempotent. Never touches artifacts.
   */
  async markReleased(params: {
    agentId: string;
    generation: string;
  }): Promise<AgentScratchReleaseReceipt & AgentRuntimeLaunchPaths> {
    const validatedAgentId = validateAgentId(params.agentId, "markReleased");
    const validatedGeneration = validateGeneration(params.generation, "markReleased");
    const paths = this.derivePaths(validatedAgentId);

    await this.assertSafeRuntimeRoot();
    await this.assertNoSymlinkAlongPath(paths.scratchDir);
    await this.assertDirectoryExists(paths.scratchDir, "scratch");

    const existing = await this.readScratchManifest(paths.scratchManifestPath);
    if (existing.agentId !== validatedAgentId) {
      throw new AgentRuntimeStorageError(
        `markReleased: scratch manifest agentId mismatch (expected ${validatedAgentId})`,
      );
    }
    if (existing.generation !== validatedGeneration) {
      throw new AgentRuntimeStorageError(
        `markReleased: generation mismatch (expected ${validatedGeneration})`,
      );
    }

    let nextManifest: AgentScratchManifest = existing;
    if (existing.lifecycle !== "released") {
      nextManifest = {
        ...existing,
        lifecycle: "released",
        releasedAt: new Date().toISOString(),
      };
      await this.writeScratchManifestAtomic(paths.scratchManifestPath, nextManifest);
    } else if (existing.releasedAt === undefined) {
      // Defensive: a released manifest should always carry releasedAt.
      nextManifest = {
        ...existing,
        releasedAt: new Date().toISOString(),
      };
      await this.writeScratchManifestAtomic(paths.scratchManifestPath, nextManifest);
    }

    const releasedAt = nextManifest.releasedAt;
    if (!releasedAt) {
      throw new AgentRuntimeStorageError("markReleased: released manifest missing releasedAt");
    }

    return {
      agentId: validatedAgentId,
      generation: nextManifest.generation,
      scratchDir: paths.scratchDir,
      artifactsDir: paths.artifactsDir,
      scratchManifestPath: paths.scratchManifestPath,
      artifactsManifestPath: paths.artifactsManifestPath,
      lifecycle: "released",
      releasedAt,
    };
  }

  private derivePaths(agentId: string): {
    scratchDir: string;
    artifactsDir: string;
    scratchManifestPath: string;
    artifactsManifestPath: string;
  } {
    const scratchParent = path.join(this.runtimeRoot, AGENT_RUNTIME_SCRATCH_DIRNAME);
    const artifactsParent = path.join(this.runtimeRoot, AGENT_RUNTIME_ARTIFACTS_DIRNAME);
    const scratchDir = path.join(scratchParent, agentId);
    const artifactsDir = path.join(artifactsParent, agentId);

    this.assertPathUnderParent(scratchParent, scratchDir, agentId, "scratch");
    this.assertPathUnderParent(artifactsParent, artifactsDir, agentId, "artifacts");

    return {
      scratchDir,
      artifactsDir,
      scratchManifestPath: path.join(scratchDir, AGENT_RUNTIME_MANIFEST_FILENAME),
      artifactsManifestPath: path.join(artifactsDir, AGENT_RUNTIME_MANIFEST_FILENAME),
    };
  }

  private assertPathUnderParent(
    parentDir: string,
    childDir: string,
    agentId: string,
    kind: "scratch" | "artifacts",
  ): void {
    const relative = path.relative(parentDir, childDir);
    if (
      relative !== agentId ||
      relative === "" ||
      relative.startsWith("..") ||
      path.isAbsolute(relative)
    ) {
      throw new AgentRuntimeStorageError(
        `agent ${kind} path escapes runtime root for agentId ${agentId}`,
      );
    }
  }

  private async assertSafeRuntimeRoot(): Promise<void> {
    await this.assertNoSymlinkAlongPath(this.runtimeRoot);

    try {
      const stats = await fs.lstat(this.runtimeRoot);
      if (stats.isSymbolicLink()) {
        throw new AgentRuntimeStorageError(
          `runtimeRoot must not be a symlink: ${this.runtimeRoot}`,
        );
      }
      if (!stats.isDirectory()) {
        throw new AgentRuntimeStorageError(
          `runtimeRoot exists and is not a directory: ${this.runtimeRoot}`,
        );
      }
    } catch (error) {
      if (isMissingEntryError(error)) {
        await this.ensurePrivateDirectory(this.runtimeRoot);
        await this.assertNoSymlinkAlongPath(this.runtimeRoot);
        return;
      }
      if (error instanceof AgentRuntimeStorageError) {
        throw error;
      }
      throw new AgentRuntimeStorageError(
        `failed to inspect runtimeRoot ${this.runtimeRoot}: ${errorMessage(error)}`,
        { cause: error },
      );
    }
  }

  private async ensureAgentDirectory(dirPath: string): Promise<void> {
    try {
      const stats = await fs.lstat(dirPath);
      if (stats.isSymbolicLink()) {
        throw new AgentRuntimeStorageError(`agent runtime path is a symlink: ${dirPath}`);
      }
      if (!stats.isDirectory()) {
        throw new AgentRuntimeStorageError(
          `agent runtime path exists and is not a directory: ${dirPath}`,
        );
      }
      await this.chmodPrivateDirectory(dirPath);
      return;
    } catch (error) {
      if (!isMissingEntryError(error)) {
        if (error instanceof AgentRuntimeStorageError) {
          throw error;
        }
        throw new AgentRuntimeStorageError(
          `failed to inspect agent runtime path ${dirPath}: ${errorMessage(error)}`,
          { cause: error },
        );
      }
    }

    await this.ensurePrivateDirectory(dirPath);
  }

  private async assertDirectoryExists(dirPath: string, kind: string): Promise<void> {
    try {
      const stats = await fs.lstat(dirPath);
      if (stats.isSymbolicLink()) {
        throw new AgentRuntimeStorageError(`${kind} path is a symlink: ${dirPath}`);
      }
      if (!stats.isDirectory()) {
        throw new AgentRuntimeStorageError(
          `${kind} path exists and is not a directory: ${dirPath}`,
        );
      }
    } catch (error) {
      if (isMissingEntryError(error)) {
        throw new AgentRuntimeStorageError(`${kind} directory does not exist: ${dirPath}`);
      }
      if (error instanceof AgentRuntimeStorageError) {
        throw error;
      }
      throw new AgentRuntimeStorageError(
        `failed to inspect ${kind} path ${dirPath}: ${errorMessage(error)}`,
        { cause: error },
      );
    }
  }

  private async loadOrCreateScratchManifest(
    paths: {
      scratchDir: string;
      scratchManifestPath: string;
    },
    agentId: string,
  ): Promise<AgentScratchManifest> {
    try {
      const existing = await this.readScratchManifest(paths.scratchManifestPath);
      if (existing.agentId !== agentId) {
        throw new AgentRuntimeStorageError(
          `scratch manifest agentId mismatch (expected ${agentId})`,
        );
      }
      if (existing.lifecycle === "active") {
        // Daemon restart / relaunch of a still-active agent reuses the generation.
        return existing;
      }

      // Released → new launch: rotate generation and return to active without deleting
      // any scratch or artifact bytes. Atomic write makes the rotation crash-safe.
      const rotated: AgentScratchManifest = {
        schemaVersion: AGENT_RUNTIME_MANIFEST_SCHEMA_VERSION,
        agentId,
        generation: randomUUID(),
        createdAt: new Date().toISOString(),
        lifecycle: "active",
      };
      await this.writeScratchManifestAtomic(paths.scratchManifestPath, rotated);
      return rotated;
    } catch (error) {
      if (!isMissingEntryError(error)) {
        throw error;
      }
    }

    const created: AgentScratchManifest = {
      schemaVersion: AGENT_RUNTIME_MANIFEST_SCHEMA_VERSION,
      agentId,
      generation: randomUUID(),
      createdAt: new Date().toISOString(),
      lifecycle: "active",
    };
    await this.writeScratchManifestAtomic(paths.scratchManifestPath, created);
    return created;
  }

  private async ensureArtifactsManifest(
    paths: {
      artifactsManifestPath: string;
    },
    agentId: string,
  ): Promise<AgentArtifactsManifest> {
    try {
      const existing = await this.readArtifactsManifest(paths.artifactsManifestPath);
      if (existing.agentId !== agentId) {
        throw new AgentRuntimeStorageError(
          `artifacts manifest agentId mismatch (expected ${agentId})`,
        );
      }
      if (existing.retention !== "retained") {
        throw new AgentRuntimeStorageError(
          `artifacts manifest retention must be "retained" for agent ${agentId}`,
        );
      }
      return existing;
    } catch (error) {
      if (!isMissingEntryError(error)) {
        throw error;
      }
    }

    const created: AgentArtifactsManifest = {
      schemaVersion: AGENT_RUNTIME_MANIFEST_SCHEMA_VERSION,
      agentId,
      retention: "retained",
      createdAt: new Date().toISOString(),
    };
    await this.writeArtifactsManifestAtomic(paths.artifactsManifestPath, created);
    return created;
  }

  private async readScratchManifest(manifestPath: string): Promise<AgentScratchManifest> {
    let raw: string;
    try {
      raw = await fs.readFile(manifestPath, "utf8");
    } catch (error) {
      if (isMissingEntryError(error)) {
        throw error;
      }
      throw new AgentRuntimeStorageError(
        `failed to read scratch manifest ${manifestPath}: ${errorMessage(error)}`,
        { cause: error },
      );
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      throw new AgentRuntimeStorageError(
        `malformed scratch manifest JSON at ${manifestPath}: ${errorMessage(error)}`,
        { cause: error },
      );
    }

    const result = ScratchManifestSchema.safeParse(parsed);
    if (!result.success) {
      throw new AgentRuntimeStorageError(
        `malformed or mismatched scratch manifest at ${manifestPath}`,
      );
    }
    return result.data;
  }

  private async readArtifactsManifest(manifestPath: string): Promise<AgentArtifactsManifest> {
    let raw: string;
    try {
      raw = await fs.readFile(manifestPath, "utf8");
    } catch (error) {
      if (isMissingEntryError(error)) {
        throw error;
      }
      throw new AgentRuntimeStorageError(
        `failed to read artifacts manifest ${manifestPath}: ${errorMessage(error)}`,
        { cause: error },
      );
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      throw new AgentRuntimeStorageError(
        `malformed artifacts manifest JSON at ${manifestPath}: ${errorMessage(error)}`,
        { cause: error },
      );
    }

    const result = ArtifactsManifestSchema.safeParse(parsed);
    if (!result.success) {
      throw new AgentRuntimeStorageError(
        `malformed or mismatched artifacts manifest at ${manifestPath}`,
      );
    }
    return result.data;
  }

  private async writeScratchManifestAtomic(
    manifestPath: string,
    manifest: AgentScratchManifest,
  ): Promise<void> {
    await writeFileAtomic(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
    await this.chmodPrivateFile(manifestPath);
  }

  private async writeArtifactsManifestAtomic(
    manifestPath: string,
    manifest: AgentArtifactsManifest,
  ): Promise<void> {
    await writeFileAtomic(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
    await this.chmodPrivateFile(manifestPath);
  }

  private async ensurePrivateDirectory(dirPath: string): Promise<void> {
    await fs.mkdir(dirPath, { recursive: true, mode: PRIVATE_DIRECTORY_MODE });
    await this.chmodPrivateDirectory(dirPath);
  }

  private async chmodPrivateDirectory(dirPath: string): Promise<void> {
    if (process.platform === "win32") {
      return;
    }
    try {
      await fs.chmod(dirPath, PRIVATE_DIRECTORY_MODE);
    } catch (error) {
      throw new AgentRuntimeStorageError(
        `failed to set private mode on directory ${dirPath}: ${errorMessage(error)}`,
        { cause: error },
      );
    }
  }

  private async chmodPrivateFile(filePath: string): Promise<void> {
    if (process.platform === "win32") {
      return;
    }
    try {
      await fs.chmod(filePath, PRIVATE_FILE_MODE);
    } catch (error) {
      throw new AgentRuntimeStorageError(
        `failed to set private mode on file ${filePath}: ${errorMessage(error)}`,
        { cause: error },
      );
    }
  }

  /**
   * Fail closed if any existing path component from the filesystem root down to
   * `targetPath` is a symbolic link.
   */
  private async assertNoSymlinkAlongPath(targetPath: string): Promise<void> {
    const resolved = path.resolve(targetPath);
    const segments = resolved.split(path.sep).filter((segment) => segment.length > 0);

    let current =
      path.win32.isAbsolute(resolved) && /^[A-Za-z]:/.test(segments[0] ?? "")
        ? `${segments.shift()}${path.sep}`
        : path.sep;

    for (const segment of segments) {
      current = path.join(current, segment);
      try {
        const stats = await fs.lstat(current);
        if (stats.isSymbolicLink()) {
          throw new AgentRuntimeStorageError(
            `agent runtime path component is a symlink: ${current}`,
          );
        }
      } catch (error) {
        if (isMissingEntryError(error)) {
          return;
        }
        if (error instanceof AgentRuntimeStorageError) {
          throw error;
        }
        throw new AgentRuntimeStorageError(
          `failed to inspect path component ${current}: ${errorMessage(error)}`,
          { cause: error },
        );
      }
    }
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
