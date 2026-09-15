import { AsyncLocalStorage } from "node:async_hooks";
import { existsSync } from "node:fs";
import pLimit from "p-limit";
import type { Logger } from "pino";
import type { ProcessEnvRecord } from "../server/paseo-env.js";
import { spawnProcess } from "./spawn.js";

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_MAX_OUTPUT_BYTES = 20 * 1024 * 1024; // 20MB
const DEFAULT_STDERR_LIMIT = 2048;

function readGitConcurrency(): number {
  return parseInt(process.env.PASEO_GIT_CONCURRENCY ?? "2", 10) || 2;
}

// Worktree creation gets one reserved slot, independent of the existing
// background limit. Total subprocess concurrency is bounded by
// PASEO_GIT_CONCURRENCY + 1; ordinary callers retain their existing capacity.
const foregroundGitLaneStorage = new AsyncLocalStorage<boolean>();
const gitConcurrencyLimit = readGitConcurrency();
const backgroundGitLimit = pLimit(gitConcurrencyLimit);
const foregroundGitLimit = pLimit(1);

/**
 * Runs `callback` in the foreground Git scheduling lane. Git commands issued
 * inside the callback use the reserved foreground slot instead of competing
 * with background status scans. Nesting is idempotent. The lane carries no
 * timer or timeout: queued creation work keeps its place rather than rejecting
 * behind a deadline that a later mutation could bypass.
 */
export function runWithForegroundGitLane<T>(callback: () => T): T {
  if (foregroundGitLaneStorage.getStore() !== undefined) {
    return callback();
  }
  return foregroundGitLaneStorage.run(true, callback);
}

export interface GitCommandOptions {
  cwd: string;
  env?: ProcessEnvRecord;
  envOverlay?: ProcessEnvRecord;
  logger?: Pick<Logger, "trace">;
  timeout?: number;
  maxOutputBytes?: number;
  acceptExitCodes?: number[];
}

export interface GitCommandResult {
  stdout: string;
  stderr: string;
  truncated: boolean;
  exitCode: number | null;
  signal: NodeJS.Signals | null;
}

export interface GitCommandMetric {
  args: string[];
  cwd: string;
  startedAtMs: number;
  durationMs: number;
  queueWaitMs: number;
  totalDurationMs: number;
  exitCode: number | null;
  signal: NodeJS.Signals | null;
  success: boolean;
}

export interface GitCommandMetricsSnapshot {
  commands: GitCommandMetric[];
  total: number;
  failed: number;
  maxConcurrent: number;
}

interface GitCommandMetricsState {
  commands: GitCommandMetric[];
  active: number;
  maxConcurrent: number;
}

let gitCommandMetricsState: GitCommandMetricsState | null = null;

export function startGitCommandMetrics(): void {
  gitCommandMetricsState = {
    commands: [],
    active: 0,
    maxConcurrent: 0,
  };
}

export function stopGitCommandMetrics(): GitCommandMetricsSnapshot {
  const state = gitCommandMetricsState;
  gitCommandMetricsState = null;
  if (!state) {
    return {
      commands: [],
      total: 0,
      failed: 0,
      maxConcurrent: 0,
    };
  }
  return {
    commands: [...state.commands],
    total: state.commands.length,
    failed: state.commands.filter((command) => !command.success).length,
    maxConcurrent: state.maxConcurrent,
  };
}

function beginGitCommandMetric(): GitCommandMetricsState | null {
  const state = gitCommandMetricsState;
  if (!state) {
    return null;
  }
  state.active += 1;
  state.maxConcurrent = Math.max(state.maxConcurrent, state.active);
  return state;
}

function finishGitCommandMetric(
  state: GitCommandMetricsState | null,
  metric: GitCommandMetric,
): void {
  if (!state) {
    return;
  }
  state.active = Math.max(0, state.active - 1);
  state.commands.push(metric);
}

function mergeEnvOverlays(
  env: ProcessEnvRecord | undefined,
  envOverlay: ProcessEnvRecord | undefined,
): ProcessEnvRecord | undefined {
  if (!env) {
    return envOverlay;
  }
  if (!envOverlay) {
    return env;
  }
  return { ...env, ...envOverlay };
}

function getEnvOverlayKeys(envOverlay: ProcessEnvRecord | undefined): string[] {
  return Object.keys(envOverlay ?? {}).sort();
}

export function runGitCommand(
  args: string[],
  options: GitCommandOptions,
): Promise<GitCommandResult> {
  const enqueuedAt = Date.now();
  const limit =
    foregroundGitLaneStorage.getStore() !== undefined ? foregroundGitLimit : backgroundGitLimit;
  return limit(() => {
    const queueWaitMs = Date.now() - enqueuedAt;
    return new Promise<GitCommandResult>((resolve, reject) => {
      const timeout = options.timeout ?? DEFAULT_TIMEOUT_MS;
      const maxOutputBytes = options.maxOutputBytes ?? DEFAULT_MAX_OUTPUT_BYTES;
      const acceptExitCodes = options.acceptExitCodes ?? [0];
      const command = formatGitCommand(args);
      const envOverlay = mergeEnvOverlays(options.env, options.envOverlay);
      const startedAt = Date.now();
      const metricsState = beginGitCommandMetric();
      const logger = typeof options.logger?.trace === "function" ? options.logger : undefined;
      const traceContext = logger
        ? {
            command: "git",
            args,
            cwd: options.cwd,
            cwdExists: existsSync(options.cwd),
            timeout,
            queueWaitMs,
            maxOutputBytes,
            acceptExitCodes,
            envOverlayKeys: getEnvOverlayKeys(envOverlay),
          }
        : null;

      if (logger && traceContext) {
        logger.trace(traceContext, "Spawning git command");
      }

      // `core.quotepath=false` makes git emit raw UTF-8 paths instead of
      // octal-escaping non-ASCII bytes (e.g. `测试文件.txt` vs `"\346\265\213..."`).
      // `core.preloadIndex=false` keeps status scans single-threaded so periodic
      // scans do not dominate blocked I/O on encrypted storage.
      const child = spawnProcess(
        "git",
        ["-c", "core.quotepath=false", "-c", "core.preloadIndex=false", ...args],
        {
          cwd: options.cwd,
          envOverlay,
          shell: false,
          stdio: ["ignore", "pipe", "pipe"],
        },
      );

      let settled = false;
      let metricFinished = false;
      let truncated = false;
      let stdoutBytes = 0;
      let stderrBytes = 0;
      const stdoutChunks: Buffer[] = [];
      const stderrChunks: Buffer[] = [];

      const settle = (callback: () => void) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        callback();
      };

      const finishMetricOnce = (
        metric: Omit<GitCommandMetric, "queueWaitMs" | "totalDurationMs">,
      ) => {
        if (metricFinished) return;
        metricFinished = true;
        finishGitCommandMetric(metricsState, {
          ...metric,
          queueWaitMs,
          totalDurationMs: Date.now() - enqueuedAt,
        });
      };

      const timer = setTimeout(() => {
        const error = new Error(`Git command timed out after ${timeout}ms: ${command}`);
        child.kill("SIGKILL");
        finishMetricOnce({
          args,
          cwd: options.cwd,
          startedAtMs: startedAt,
          durationMs: Date.now() - startedAt,
          exitCode: null,
          signal: "SIGKILL",
          success: false,
        });
        settle(() => reject(error));
      }, timeout);

      child.stdout!.on("data", (chunk: Buffer | string) => {
        if (settled || truncated) return;

        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        const remainingBytes = maxOutputBytes - stdoutBytes;

        if (remainingBytes <= 0) {
          truncated = true;
          child.kill("SIGKILL");
          return;
        }

        if (buffer.length > remainingBytes) {
          stdoutChunks.push(buffer.subarray(0, remainingBytes));
          stdoutBytes += remainingBytes;
          truncated = true;
          child.kill("SIGKILL");
          return;
        }

        stdoutChunks.push(buffer);
        stdoutBytes += buffer.length;
      });

      child.stderr!.on("data", (chunk: Buffer | string) => {
        if (settled || stderrBytes >= DEFAULT_STDERR_LIMIT) return;

        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        const remainingBytes = DEFAULT_STDERR_LIMIT - stderrBytes;

        if (buffer.length > remainingBytes) {
          stderrChunks.push(buffer.subarray(0, remainingBytes));
          stderrBytes += remainingBytes;
          return;
        }

        stderrChunks.push(buffer);
        stderrBytes += buffer.length;
      });

      child.on("error", (error) => {
        finishMetricOnce({
          args,
          cwd: options.cwd,
          startedAtMs: startedAt,
          durationMs: Date.now() - startedAt,
          exitCode: null,
          signal: null,
          success: false,
        });
        if (logger && traceContext) {
          logger.trace(
            {
              ...traceContext,
              err: error,
              durationMs: Date.now() - startedAt,
              totalDurationMs: Date.now() - enqueuedAt,
            },
            "Git command process error",
          );
        }
        settle(() => reject(error));
      });

      child.on("close", (exitCode, signal) => {
        const result: GitCommandResult = {
          stdout: Buffer.concat(stdoutChunks).toString("utf8"),
          stderr: Buffer.concat(stderrChunks).toString("utf8"),
          truncated,
          exitCode,
          signal,
        };
        if (logger && traceContext) {
          logger.trace(
            {
              ...traceContext,
              durationMs: Date.now() - startedAt,
              totalDurationMs: Date.now() - enqueuedAt,
              exitCode,
              signal,
              truncated,
              stdoutBytes,
              stderrBytes,
            },
            "Git command closed",
          );
        }

        if (!truncated && !acceptExitCodes.includes(exitCode ?? -1)) {
          finishMetricOnce({
            args,
            cwd: options.cwd,
            startedAtMs: startedAt,
            durationMs: Date.now() - startedAt,
            exitCode,
            signal,
            success: false,
          });
          const stderrPreview = result.stderr.trim() || "(no stderr)";
          const truncationNote = result.truncated ? " (stdout truncated)" : "";

          settle(() =>
            reject(
              new Error(
                `Git command failed: ${command}${truncationNote} (exit code: ${String(exitCode)}, signal: ${signal ?? "none"})\n${stderrPreview}`,
              ),
            ),
          );
          return;
        }

        finishMetricOnce({
          args,
          cwd: options.cwd,
          startedAtMs: startedAt,
          durationMs: Date.now() - startedAt,
          exitCode,
          signal,
          success: true,
        });
        settle(() => resolve(result));
      });
    });
  });
}

function formatGitCommand(args: string[]): string {
  return ["git", ...args].join(" ");
}
