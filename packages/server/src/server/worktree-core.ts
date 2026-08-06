import { existsSync } from "node:fs";
import { statfs } from "node:fs/promises";
import path from "node:path";
import { createNameId } from "mnemonic-id";

import type { ForgeService } from "../services/forge-service.js";
import {
  createWorktree,
  resolveExistingWorktreeForSlug,
  resolvePaseoWorktreesBaseRoot,
  slugify,
  validateBranchSlug,
  type WorktreeConfig,
} from "../utils/worktree.js";
import {
  resolveWorktreeCreationIntent,
  type ResolveWorktreeCreationIntentInput,
  UnsupportedForgeCheckoutTargetError,
  type WorktreeCreationIntent,
} from "./resolve-worktree-creation-intent.js";
import type { ChangeRequestCheckoutSource, FirstAgentContext } from "@getpaseo/protocol/messages";
import type { WorkspaceGitService } from "./workspace-git-service.js";

export interface CreateWorktreeCoreInput {
  cwd: string;
  worktreeSlug?: string;
  branchName?: string;
  refName?: string;
  action?: "branch-off" | "checkout";
  checkoutSource?: ChangeRequestCheckoutSource;
  githubPrNumber?: number;
  firstAgentContext?: FirstAgentContext;
  paseoHome?: string;
  worktreesRoot?: string;
  /**
   * Optional free-space floor for new worktree creation only.
   * Unset skips the admission guard (upstream behavior). Equality passes.
   */
  minimumFreeBytes?: number;
  runSetup?: boolean;
}

export interface CreateWorktreeCoreDeps {
  github: ForgeService;
  workspaceGitService?: Pick<
    WorkspaceGitService,
    "resolveRepoRoot" | "resolveDefaultBranch" | "resolveForge"
  >;
  resolveDefaultBranch?: (repoRoot: string) => Promise<string>;
  /**
   * Optional free-space probe (bytes available on the checked path).
   * Tests inject this; production uses Node `statfs`.
   */
  getAvailableBytes?: (checkedPath: string) => Promise<number>;
  /**
   * Optional create primitive override for tests that assert no mutation on refusal.
   * Defaults to the real `createWorktree` helper.
   */
  createWorktree?: typeof createWorktree;
}

export interface CreateWorktreeCoreResult {
  worktree: WorktreeConfig;
  intent: WorktreeCreationIntent;
  repoRoot: string;
  created: boolean;
}

/**
 * Fail-closed free-space admission failure for new worktree creation.
 * Includes measured available bytes, the configured floor, and the path that was checked
 * (worktrees root or its nearest existing ancestor).
 */
export class InsufficientWorktreeFreeSpaceError extends Error {
  readonly availableBytes: number;
  readonly requiredBytes: number;
  readonly checkedPath: string;

  constructor(params: { availableBytes: number; requiredBytes: number; checkedPath: string }) {
    super(
      `Refusing to create worktree: only ${params.availableBytes} free bytes at ${params.checkedPath}; need at least ${params.requiredBytes} free bytes`,
    );
    this.name = "InsufficientWorktreeFreeSpaceError";
    this.availableBytes = params.availableBytes;
    this.requiredBytes = params.requiredBytes;
    this.checkedPath = params.checkedPath;
  }
}

/**
 * Fail-closed probe failure: filesystem root could not be resolved or `statfs` failed
 * while a free-space floor was configured.
 */
export class WorktreeFreeSpaceProbeError extends Error {
  readonly checkedPath: string;

  constructor(params: { checkedPath: string; cause?: unknown }) {
    let reason = "unknown error";
    if (params.cause instanceof Error) {
      reason = params.cause.message;
    } else if (params.cause !== undefined) {
      reason = String(params.cause);
    }
    super(
      `Refusing to create worktree: could not determine free space at ${params.checkedPath}: ${reason}`,
    );
    this.name = "WorktreeFreeSpaceProbeError";
    this.checkedPath = params.checkedPath;
    if (params.cause instanceof Error) {
      this.cause = params.cause;
    }
  }
}

/**
 * Walk to the nearest existing ancestor of `targetPath` without creating anything.
 * Used so a not-yet-created worktrees root still admits against the filesystem that
 * will hold it.
 */
export function resolveNearestExistingAncestorPath(targetPath: string): string {
  let current = path.resolve(targetPath);
  while (!existsSync(current)) {
    const parent = path.dirname(current);
    if (parent === current) {
      throw new WorktreeFreeSpaceProbeError({
        checkedPath: current,
        cause: new Error("no existing ancestor path"),
      });
    }
    current = parent;
  }
  return current;
}

export async function probeAvailableBytes(checkedPath: string): Promise<number> {
  try {
    const stats = await statfs(checkedPath);
    const availableBytes = Number(stats.bavail) * Number(stats.bsize);
    if (!Number.isFinite(availableBytes) || availableBytes < 0) {
      throw new Error("invalid free-space measurement");
    }
    return availableBytes;
  } catch (error) {
    if (error instanceof WorktreeFreeSpaceProbeError) {
      throw error;
    }
    throw new WorktreeFreeSpaceProbeError({ checkedPath, cause: error });
  }
}

/**
 * Fail-closed free-space admission for new worktree creation.
 * No-op when `minimumFreeBytes` is unset. Equality (`available === required`) passes.
 * Does not create directories or mutate worktrees.
 */
export async function assertWorktreeCreateFreeSpace(options: {
  minimumFreeBytes?: number;
  paseoHome?: string;
  worktreesRoot?: string;
  getAvailableBytes?: (checkedPath: string) => Promise<number>;
}): Promise<void> {
  const minimumFreeBytes = options.minimumFreeBytes;
  if (minimumFreeBytes === undefined) {
    return;
  }

  const worktreesBaseRoot = resolvePaseoWorktreesBaseRoot({
    paseoHome: options.paseoHome,
    worktreesRoot: options.worktreesRoot,
  });

  let checkedPath: string;
  try {
    checkedPath = resolveNearestExistingAncestorPath(worktreesBaseRoot);
  } catch (error) {
    if (error instanceof WorktreeFreeSpaceProbeError) {
      throw error;
    }
    throw new WorktreeFreeSpaceProbeError({ checkedPath: worktreesBaseRoot, cause: error });
  }

  const getAvailableBytes = options.getAvailableBytes ?? probeAvailableBytes;
  let availableBytes: number;
  try {
    availableBytes = await getAvailableBytes(checkedPath);
  } catch (error) {
    if (error instanceof WorktreeFreeSpaceProbeError) {
      throw error;
    }
    throw new WorktreeFreeSpaceProbeError({ checkedPath, cause: error });
  }

  if (availableBytes < minimumFreeBytes) {
    throw new InsufficientWorktreeFreeSpaceError({
      availableBytes,
      requiredBytes: minimumFreeBytes,
      checkedPath,
    });
  }
}

export async function createWorktreeCore(
  input: CreateWorktreeCoreInput,
  deps: CreateWorktreeCoreDeps,
): Promise<CreateWorktreeCoreResult> {
  const repoRoot = await resolveWorktreeRepoRoot(input, deps.workspaceGitService);
  const requestedWorktreeSlug = input.worktreeSlug
    ? normalizeWorktreeSlug(input.worktreeSlug)
    : undefined;
  const requestedBranchName = input.branchName
    ? validateWorktreeSlug(input.branchName.trim())
    : undefined;

  let intentInput: ResolveWorktreeCreationIntentInput;
  if (input.action === "checkout") {
    intentInput = {
      action: "checkout",
      refName: input.refName,
      checkoutSource: input.checkoutSource,
      githubPrNumber: input.githubPrNumber,
      worktreeSlug: requestedWorktreeSlug,
    };
  } else if (input.checkoutSource !== undefined || input.githubPrNumber !== undefined) {
    intentInput = {
      checkoutSource: input.checkoutSource,
      githubPrNumber: input.githubPrNumber,
      refName: input.refName,
      worktreeSlug: requestedWorktreeSlug,
    };
  } else {
    const worktreeSlug = requestedWorktreeSlug ?? normalizeWorktreeSlug(createNameId());
    intentInput = {
      action: "branch-off",
      refName: input.refName,
      branchName: requestedBranchName,
      worktreeSlug,
    };
  }

  const forge = await resolveForge(repoRoot, deps, intentInput);
  const intent = await resolveWorktreeCreationIntent(intentInput, repoRoot, {
    forge: forge.forge,
    forgeService: forge.service,
    resolveDefaultBranch: (root) => resolveDefaultBranch(root, deps),
  });
  let normalizedSlug: string;

  switch (intent.kind) {
    case "branch-off": {
      normalizedSlug = requestedWorktreeSlug ?? normalizeWorktreeSlug(intent.branchName);
      break;
    }
    case "checkout-branch": {
      normalizedSlug = requestedWorktreeSlug ?? normalizeWorktreeSlug(intent.branchName);
      break;
    }
    case "checkout-change-request":
    case "checkout-github-pr": {
      normalizedSlug =
        requestedWorktreeSlug ?? normalizeWorktreeSlug(intent.localBranchName ?? intent.headRef);
      break;
    }
  }

  const existingWorktree = await resolveExistingWorktreeForSlug({
    slug: normalizedSlug,
    repoRoot,
    paseoHome: input.paseoHome,
    worktreesRoot: input.worktreesRoot,
  });
  if (existingWorktree) {
    return { worktree: existingWorktree, intent, repoRoot, created: false };
  }

  // New creation only: admit against free space before any worktree/git mutation.
  await assertWorktreeCreateFreeSpace({
    minimumFreeBytes: input.minimumFreeBytes,
    paseoHome: input.paseoHome,
    worktreesRoot: input.worktreesRoot,
    getAvailableBytes: deps.getAvailableBytes,
  });

  const createWorktreeFn = deps.createWorktree ?? createWorktree;
  return {
    worktree: await createWorktreeFn({
      cwd: repoRoot,
      worktreeSlug: normalizedSlug,
      source: intent,
      runSetup: input.runSetup ?? true,
      paseoHome: input.paseoHome,
      worktreesRoot: input.worktreesRoot,
    }),
    intent,
    repoRoot,
    created: true,
  };
}

async function resolveForge(
  repoRoot: string,
  deps: CreateWorktreeCoreDeps,
  intentInput: ResolveWorktreeCreationIntentInput,
): Promise<{ forge: string; service: ForgeService }> {
  const resolution = await deps.workspaceGitService?.resolveForge(repoRoot);
  if (!resolution) {
    if (intentInput.checkoutSource?.forge && intentInput.checkoutSource.forge !== "github") {
      throw new UnsupportedForgeCheckoutTargetError(intentInput.checkoutSource.forge);
    }
    // No recognized remote: fall back to GitHub, the wire-default forge.
    return { forge: "github", service: deps.github };
  }
  return { forge: resolution.forge, service: resolution.service };
}

async function resolveDefaultBranch(
  repoRoot: string,
  deps: CreateWorktreeCoreDeps,
): Promise<string> {
  const baseBranch = deps.resolveDefaultBranch
    ? await deps.resolveDefaultBranch(repoRoot)
    : await deps.workspaceGitService?.resolveDefaultBranch(repoRoot);
  if (!baseBranch) {
    throw new Error("Unable to resolve repository default branch");
  }
  return baseBranch;
}

export async function resolveWorktreeRepoRoot(
  input: Pick<CreateWorktreeCoreInput, "cwd" | "paseoHome">,
  workspaceGitService?: Pick<WorkspaceGitService, "resolveRepoRoot">,
): Promise<string> {
  if (!workspaceGitService) {
    throw new Error("Create worktree requires WorkspaceGitService");
  }

  return workspaceGitService.resolveRepoRoot(input.cwd);
}

function validateWorktreeSlug(slug: string): string {
  const validation = validateBranchSlug(slug);
  if (!validation.valid) {
    throw new Error(`Invalid worktree name: ${validation.error}`);
  }
  return slug;
}

function normalizeWorktreeSlug(value: string): string {
  return validateWorktreeSlug(slugify(value));
}
