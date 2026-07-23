import path from "node:path";
import type { Command } from "commander";
import type { DaemonClient } from "@getpaseo/client/internal/daemon-client";
import { connectToDaemon, getDaemonHost } from "../../utils/client.js";
import type { CommandError, OutputSchema, SingleResult } from "../../output/index.js";
import { parseDuration } from "../../utils/duration.js";
import { buildCreateWorktreeRequest, type WorktreeCreateOptions } from "./create-input.js";

export interface WorktreeCreateResult {
  workspaceId: string;
  name: string;
  branchName: string;
  worktreePath: string;
}

export const createSchema: OutputSchema<WorktreeCreateResult> = {
  idField: "worktreePath",
  columns: [
    { header: "NAME", field: "name", width: 24 },
    { header: "BRANCH", field: "branchName", width: 28 },
    { header: "PATH", field: "worktreePath", width: 50 },
  ],
};

interface CreateCommandDependencies {
  connectToDaemon: typeof connectToDaemon;
  now: () => number;
  sleep: (milliseconds: number) => Promise<void>;
  settleWithin: <T>(
    promise: Promise<T>,
    timeoutMs: number,
  ) => Promise<{ timedOut: true } | { timedOut: false; value: T }>;
}

const DEFAULT_SETUP_WAIT_TIMEOUT = "30m";
const SETUP_POLL_INTERVAL_MS = 250;

function cmdError(code: string, message: string, details?: string): CommandError {
  return details ? { code, message, details } : { code, message };
}

function setupTimeoutError(workspaceId: string, timeoutMs: number): CommandError {
  return cmdError(
    "WORKTREE_SETUP_TIMEOUT",
    `Worktree setup did not finish within ${Math.ceil(timeoutMs / 1000)} seconds`,
    `The managed worktree remains registered as workspace ${workspaceId}; inspect its setup status before using or archiving it.`,
  );
}

function resolveSetupWaitTimeout(options: WorktreeCreateOptions): number {
  const rawTimeout = options.waitTimeout ?? DEFAULT_SETUP_WAIT_TIMEOUT;
  try {
    const timeoutMs = parseDuration(rawTimeout);
    if (timeoutMs <= 0) {
      throw new Error("Timeout must be positive");
    }
    return timeoutMs;
  } catch (error) {
    throw cmdError(
      "INVALID_WAIT_TIMEOUT",
      `Invalid --wait-timeout: ${rawTimeout}`,
      error instanceof Error ? error.message : String(error),
    );
  }
}

async function waitForWorkspaceSetup(
  client: DaemonClient,
  workspaceId: string,
  timeoutMs: number,
  dependencies: Pick<CreateCommandDependencies, "now" | "sleep" | "settleWithin">,
): Promise<void> {
  const startedAt = dependencies.now();

  while (true) {
    const elapsedBeforeRequestMs = dependencies.now() - startedAt;
    const remainingBeforeRequestMs = timeoutMs - elapsedBeforeRequestMs;
    if (remainingBeforeRequestMs <= 0) {
      throw setupTimeoutError(workspaceId, timeoutMs);
    }

    const statusResult = await dependencies.settleWithin(
      client.fetchWorkspaceSetupStatus(workspaceId),
      remainingBeforeRequestMs,
    );
    if (statusResult.timedOut) {
      throw setupTimeoutError(workspaceId, timeoutMs);
    }

    const response = statusResult.value;
    const snapshot = response.snapshot;
    if (snapshot?.status === "completed") {
      return;
    }
    if (snapshot?.status === "failed") {
      throw cmdError(
        "WORKTREE_SETUP_FAILED",
        "Worktree setup failed",
        snapshot.error ?? "The setup hook reported failure",
      );
    }

    const elapsedMs = dependencies.now() - startedAt;
    if (elapsedMs >= timeoutMs) {
      throw setupTimeoutError(workspaceId, timeoutMs);
    }

    await dependencies.sleep(Math.min(SETUP_POLL_INTERVAL_MS, timeoutMs - elapsedMs));
  }
}

export async function runCreateCommandWithDeps(
  options: WorktreeCreateOptions,
  _command: Command,
  dependencies: CreateCommandDependencies,
): Promise<SingleResult<WorktreeCreateResult>> {
  const cwd = options.cwd ?? process.cwd();
  const request = buildCreateWorktreeRequest(options, cwd);
  const setupWaitTimeoutMs = options.wait ? resolveSetupWaitTimeout(options) : null;

  const host = getDaemonHost({ host: options.host });
  let client: DaemonClient;
  try {
    client = await dependencies.connectToDaemon({ host: options.host });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    throw cmdError(
      "DAEMON_NOT_RUNNING",
      `Cannot connect to daemon at ${host}: ${message}`,
      "Start the daemon with: paseo daemon start",
    );
  }

  try {
    const response = await client.createPaseoWorktree(request);

    const workspace = response.workspace;
    if (!workspace || response.error) {
      throw cmdError(
        "WORKTREE_CREATE_FAILED",
        `Failed to create worktree: ${response.error ?? "no workspace returned"}`,
      );
    }

    if (!workspace.workspaceDirectory) {
      throw cmdError(
        "WORKTREE_CREATE_FAILED",
        "Failed to create worktree: workspace directory missing from daemon response",
      );
    }
    const worktreePath = workspace.workspaceDirectory;

    if (setupWaitTimeoutMs !== null) {
      await waitForWorkspaceSetup(client, workspace.id, setupWaitTimeoutMs, dependencies);
    }

    return {
      type: "single",
      data: {
        workspaceId: workspace.id,
        name: path.basename(worktreePath),
        branchName: workspace.name,
        worktreePath,
      },
      schema: createSchema,
    };
  } catch (err) {
    if (err && typeof err === "object" && "code" in err) {
      throw err;
    }
    const message = err instanceof Error ? err.message : String(err);
    throw cmdError("WORKTREE_CREATE_FAILED", `Failed to create worktree: ${message}`);
  } finally {
    await client.close().catch(() => {});
  }
}

export async function runCreateCommand(
  options: WorktreeCreateOptions,
  command: Command,
): Promise<SingleResult<WorktreeCreateResult>> {
  return runCreateCommandWithDeps(options, command, {
    connectToDaemon,
    now: Date.now,
    sleep: (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
    settleWithin: (promise, timeoutMs) =>
      new Promise((resolve, reject) => {
        const timeout = setTimeout(() => resolve({ timedOut: true }), timeoutMs);
        promise.then(
          (value) => {
            clearTimeout(timeout);
            return resolve({ timedOut: false, value });
          },
          (error: unknown) => {
            clearTimeout(timeout);
            return reject(error);
          },
        );
      }),
  });
}
