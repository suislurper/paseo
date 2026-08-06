import { randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import path from "node:path";
import { z } from "zod";

import { writeFileAtomic } from "../atomic-file.js";
import { PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE } from "../private-files.js";

const AgentIdSchema = z.guid();
const GenerationSchema = z.string().uuid();
const LockTokenSchema = z.string().uuid();

export const AGENT_RUNTIME_MANIFEST_SCHEMA_VERSION = 1 as const;
export const AGENT_RUNTIME_LOCK_OWNER_SCHEMA_VERSION = 1 as const;
export const AGENT_RUNTIME_SCRATCH_DIRNAME = "scratch";
export const AGENT_RUNTIME_ARTIFACTS_DIRNAME = "artifacts";
export const AGENT_RUNTIME_LOCKS_DIRNAME = "locks";
export const AGENT_RUNTIME_MANIFEST_FILENAME = "manifest.json";
export const AGENT_RUNTIME_LOCK_OWNER_FILENAME = "owner.json";
export const AGENT_RUNTIME_LOCK_DIR_SUFFIX = ".lock";

/** Linux kernel boot id; optional owner metadata when readable. Never required. */
const LINUX_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id";

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

const LockOwnerSchema = z
  .object({
    schemaVersion: z.literal(AGENT_RUNTIME_LOCK_OWNER_SCHEMA_VERSION),
    agentId: AgentIdSchema,
    lockToken: LockTokenSchema,
    operation: z.string().min(1).max(64),
    pid: z.number().int().positive(),
    bootId: z.string().min(1).optional(),
    acquiredAt: z.string().min(1),
  })
  .strict();

export type AgentScratchManifest = z.infer<typeof ScratchManifestSchema>;
export type AgentArtifactsManifest = z.infer<typeof ArtifactsManifestSchema>;
export type AgentRuntimeLockOwner = z.infer<typeof LockOwnerSchema>;

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

/**
 * Safe context handed to a held-lock callback. Paths are exact derived locations under
 * the configured runtime root — not an arbitrary path API.
 */
export interface AgentRuntimeLockHeldContext {
  agentId: string;
  operation: string;
  scratchDir: string;
  artifactsDir: string;
  scratchManifestPath: string;
  artifactsManifestPath: string;
  /** Present when a valid scratch manifest already exists; null when missing. */
  scratchManifest: AgentScratchManifest | null;
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

interface HeldAgentLock {
  agentId: string;
  lockDir: string;
  lockToken: string;
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

function validateOperation(operation: string, source: string): string {
  const trimmed = operation.trim();
  if (!trimmed || trimmed.length > 64) {
    throw new AgentRuntimeStorageError(
      `${source}: operation must be a non-empty string of at most 64 characters`,
    );
  }
  if (trimmed.includes("/") || trimmed.includes("\\") || trimmed.includes("\0")) {
    throw new AgentRuntimeStorageError(`${source}: operation contains invalid characters`);
  }
  return trimmed;
}

function isErrnoCode(error: unknown, code: string): boolean {
  return (error as NodeJS.ErrnoException | null)?.code === code;
}

/**
 * Durable per-agent runtime storage under a configured absolute runtime root.
 *
 * Layout:
 *   {runtimeRoot}/scratch/{agentId}/manifest.json
 *   {runtimeRoot}/artifacts/{agentId}/manifest.json
 *   {runtimeRoot}/locks/{agentId}.lock/owner.json   # cross-process metadata lock
 *
 * Scratch is generation-scoped launch state. Artifacts are retained and are never
 * touched by release. This primitive does not delete scratch/artifact files.
 *
 * prepare and markReleased hold the per-agent lock only for metadata work — never
 * across provider launch/run. A crash may leave a stale lock directory; acquisition
 * never breaks locks by age and blocks until explicit operator inspection.
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
   * Acquire the exact per-agent filesystem lock, run `callback` with validated paths
   * and the current scratch manifest (if any), then release the lock in `finally`.
   *
   * Intended surface for prepare, release, and future cleanup. Callbacks receive only
   * derived exact paths — not a raw arbitrary path API.
   */
  async withAgentLock<T>(
    params: { agentId: string; operation: string },
    callback: (context: AgentRuntimeLockHeldContext) => Promise<T>,
  ): Promise<T> {
    const validatedAgentId = validateAgentId(params.agentId, "withAgentLock");
    const operation = validateOperation(params.operation, "withAgentLock");
    const paths = this.derivePaths(validatedAgentId);
    const held = await this.acquireAgentLock(validatedAgentId, operation);
    try {
      const scratchManifest = await this.readScratchManifestIfPresent(paths.scratchManifestPath);
      return await callback({
        agentId: validatedAgentId,
        operation,
        scratchDir: paths.scratchDir,
        artifactsDir: paths.artifactsDir,
        scratchManifestPath: paths.scratchManifestPath,
        artifactsManifestPath: paths.artifactsManifestPath,
        scratchManifest,
      });
    } finally {
      await this.releaseAgentLock(held);
    }
  }

  /**
   * Ensure scratch + artifacts directories exist for the agent, create or reuse the
   * daemon-owned scratch generation manifest, and ensure the retained artifacts marker.
   *
   * - Active generation: reused across daemon restarts (idempotent prepare).
   * - Released generation: atomically rotated to a fresh active generation. Bytes under
   *   scratch/artifacts are retained; only the scratch manifest generation + lifecycle
   *   change. This prevents a stale release receipt from authorizing cleanup after relaunch.
   *
   * Holds the per-agent lock only for this metadata work — not across provider launch.
   */
  async prepare(agentId: string): Promise<AgentRuntimeLaunchPaths> {
    const validatedAgentId = validateAgentId(agentId, "prepare");
    return this.withAgentLock(
      { agentId: validatedAgentId, operation: "prepare" },
      async (context) => {
        const paths = {
          scratchDir: context.scratchDir,
          artifactsDir: context.artifactsDir,
          scratchManifestPath: context.scratchManifestPath,
          artifactsManifestPath: context.artifactsManifestPath,
        };

        await this.ensurePrivateDirectory(
          path.join(this.runtimeRoot, AGENT_RUNTIME_SCRATCH_DIRNAME),
        );
        await this.ensurePrivateDirectory(
          path.join(this.runtimeRoot, AGENT_RUNTIME_ARTIFACTS_DIRNAME),
        );
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
      },
    );
  }

  /**
   * Mark a scratch generation released. Requires exact agent ID + generation.
   * Repeated exact release is idempotent. Never touches artifacts.
   *
   * Holds the per-agent lock only for this metadata work — not across provider run.
   */
  async markReleased(params: {
    agentId: string;
    generation: string;
  }): Promise<AgentScratchReleaseReceipt & AgentRuntimeLaunchPaths> {
    const validatedAgentId = validateAgentId(params.agentId, "markReleased");
    const validatedGeneration = validateGeneration(params.generation, "markReleased");

    return this.withAgentLock(
      { agentId: validatedAgentId, operation: "release" },
      async (context) => {
        const paths = {
          scratchDir: context.scratchDir,
          artifactsDir: context.artifactsDir,
          scratchManifestPath: context.scratchManifestPath,
          artifactsManifestPath: context.artifactsManifestPath,
        };

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
          lifecycle: "released" as const,
          releasedAt,
        };
      },
    );
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

  private deriveLockDir(agentId: string): string {
    const locksParent = path.join(this.runtimeRoot, AGENT_RUNTIME_LOCKS_DIRNAME);
    const lockDirName = `${agentId}${AGENT_RUNTIME_LOCK_DIR_SUFFIX}`;
    const lockDir = path.join(locksParent, lockDirName);
    this.assertPathUnderParent(locksParent, lockDir, lockDirName, "lock");
    return lockDir;
  }

  private assertPathUnderParent(
    parentDir: string,
    childDir: string,
    expectedRelative: string,
    kind: string,
  ): void {
    const relative = path.relative(parentDir, childDir);
    if (
      relative !== expectedRelative ||
      relative === "" ||
      relative.startsWith("..") ||
      path.isAbsolute(relative)
    ) {
      throw new AgentRuntimeStorageError(
        `agent ${kind} path escapes runtime root for ${expectedRelative}`,
      );
    }
  }

  /**
   * Atomic cross-process lock via mkdir. A present, malformed, or symlink lock fails
   * closed — never broken by age. Crash-left locks intentionally block until operator
   * inspection.
   */
  private async acquireAgentLock(agentId: string, operation: string): Promise<HeldAgentLock> {
    await this.assertSafeRuntimeRoot();

    const locksParent = path.join(this.runtimeRoot, AGENT_RUNTIME_LOCKS_DIRNAME);
    await this.ensurePrivateDirectory(locksParent);
    await this.assertNoSymlinkAlongPath(locksParent);

    const lockDir = this.deriveLockDir(agentId);
    await this.assertNoSymlinkAlongPath(path.dirname(lockDir));

    try {
      const existing = await fs.lstat(lockDir);
      if (existing.isSymbolicLink()) {
        throw new AgentRuntimeStorageError(`agent lock path is a symlink: ${lockDir}`);
      }
      // Present directory, file, or anything else — fail closed; never break by age.
      throw new AgentRuntimeStorageError(
        `agent lock already held or present for ${agentId} (stale locks require operator inspection)`,
      );
    } catch (error) {
      if (error instanceof AgentRuntimeStorageError) {
        throw error;
      }
      if (!isMissingEntryError(error)) {
        throw new AgentRuntimeStorageError(
          `failed to inspect agent lock path ${lockDir}: ${errorMessage(error)}`,
          { cause: error },
        );
      }
    }

    try {
      // Atomic acquisition: mkdir without recursive is exclusive on the final component.
      await fs.mkdir(lockDir, { recursive: false, mode: PRIVATE_DIRECTORY_MODE });
    } catch (error) {
      if (isErrnoCode(error, "EEXIST")) {
        throw new AgentRuntimeStorageError(
          `agent lock already held for ${agentId} (stale locks require operator inspection)`,
        );
      }
      throw new AgentRuntimeStorageError(
        `failed to acquire agent lock for ${agentId}: ${errorMessage(error)}`,
        { cause: error },
      );
    }

    await this.chmodPrivateDirectory(lockDir);

    const lockToken = randomUUID();
    const bootId = await readOptionalBootId();
    const owner: AgentRuntimeLockOwner = {
      schemaVersion: AGENT_RUNTIME_LOCK_OWNER_SCHEMA_VERSION,
      agentId,
      lockToken,
      operation,
      pid: process.pid,
      ...(bootId !== undefined ? { bootId } : {}),
      acquiredAt: new Date().toISOString(),
    };

    try {
      await this.writeLockOwnerAtomic(path.join(lockDir, AGENT_RUNTIME_LOCK_OWNER_FILENAME), owner);
    } catch (error) {
      // Best-effort: leave the lock directory in place if owner write fails so a
      // partial acquire still fails closed for others (operator inspection).
      throw new AgentRuntimeStorageError(
        `failed to write agent lock owner for ${agentId}: ${errorMessage(error)}`,
        { cause: error },
      );
    }

    return { agentId, lockDir, lockToken };
  }

  /**
   * Release only after re-reading the exact lock token. Unexpected contents or token
   * mismatch fails closed and leaves the lock. Never recursively deletes the lock dir.
   */
  private async releaseAgentLock(held: HeldAgentLock): Promise<void> {
    const ownerPath = path.join(held.lockDir, AGENT_RUNTIME_LOCK_OWNER_FILENAME);

    try {
      const stats = await fs.lstat(held.lockDir);
      if (stats.isSymbolicLink()) {
        throw new AgentRuntimeStorageError(
          `cannot release agent lock: lock path is a symlink: ${held.lockDir}`,
        );
      }
      if (!stats.isDirectory()) {
        throw new AgentRuntimeStorageError(
          `cannot release agent lock: lock path is not a directory: ${held.lockDir}`,
        );
      }
    } catch (error) {
      if (error instanceof AgentRuntimeStorageError) {
        throw error;
      }
      if (isMissingEntryError(error)) {
        throw new AgentRuntimeStorageError(
          `cannot release agent lock: lock directory missing for ${held.agentId}`,
        );
      }
      throw new AgentRuntimeStorageError(
        `cannot release agent lock: failed to inspect ${held.lockDir}: ${errorMessage(error)}`,
        { cause: error },
      );
    }

    let raw: string;
    try {
      raw = await fs.readFile(ownerPath, "utf8");
    } catch (error) {
      throw new AgentRuntimeStorageError(
        `cannot release agent lock: owner.json missing or unreadable for ${held.agentId}`,
        { cause: error },
      );
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (error) {
      throw new AgentRuntimeStorageError(
        `cannot release agent lock: malformed owner.json for ${held.agentId}`,
        { cause: error },
      );
    }

    const ownerResult = LockOwnerSchema.safeParse(parsed);
    if (!ownerResult.success) {
      throw new AgentRuntimeStorageError(
        `cannot release agent lock: invalid owner.json for ${held.agentId}`,
      );
    }

    const owner = ownerResult.data;
    if (owner.agentId !== held.agentId || owner.lockToken !== held.lockToken) {
      throw new AgentRuntimeStorageError(
        `cannot release agent lock: token mismatch for ${held.agentId}`,
      );
    }

    let entries: string[];
    try {
      entries = await fs.readdir(held.lockDir);
    } catch (error) {
      throw new AgentRuntimeStorageError(
        `cannot release agent lock: failed to list lock directory for ${held.agentId}`,
        { cause: error },
      );
    }

    if (entries.length !== 1 || entries[0] !== AGENT_RUNTIME_LOCK_OWNER_FILENAME) {
      throw new AgentRuntimeStorageError(
        `cannot release agent lock: unexpected contents in lock directory for ${held.agentId}`,
      );
    }

    try {
      await fs.unlink(ownerPath);
    } catch (error) {
      throw new AgentRuntimeStorageError(
        `cannot release agent lock: failed to remove owner.json for ${held.agentId}`,
        { cause: error },
      );
    }

    try {
      // Non-recursive: fails closed if anything unexpected remains.
      await fs.rmdir(held.lockDir);
    } catch (error) {
      throw new AgentRuntimeStorageError(
        `cannot release agent lock: failed to remove lock directory for ${held.agentId}`,
        { cause: error },
      );
    }
  }

  private async writeLockOwnerAtomic(
    ownerPath: string,
    owner: AgentRuntimeLockOwner,
  ): Promise<void> {
    await writeFileAtomic(ownerPath, `${JSON.stringify(owner, null, 2)}\n`);
    await this.chmodPrivateFile(ownerPath);
  }

  private async readScratchManifestIfPresent(
    manifestPath: string,
  ): Promise<AgentScratchManifest | null> {
    try {
      return await this.readScratchManifest(manifestPath);
    } catch (error) {
      if (isMissingEntryError(error)) {
        return null;
      }
      throw error;
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

/** Best-effort Linux boot id for lock owner diagnostics. Never fails acquisition. */
async function readOptionalBootId(): Promise<string | undefined> {
  try {
    const value = (await fs.readFile(LINUX_BOOT_ID_PATH, "utf8")).trim();
    return value.length > 0 ? value : undefined;
  } catch {
    return undefined;
  }
}
