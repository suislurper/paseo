import { ChildProcess } from "node:child_process";
import { PassThrough } from "node:stream";
import { existsSync } from "node:fs";
import { chmod, mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { inheritLoginShellEnv } from "./login-shell-env";

const zsh = "/bin/zsh";
const describeIfZsh = existsSync(zsh) ? describe : describe.skip;
const describeIfPosix = process.platform === "win32" ? describe.skip : describe;
const basePath = "/usr/bin:/bin:/usr/sbin:/sbin";
const fakeHome = path.join(os.tmpdir(), "paseo-login-shell-env-fake-home");
type LoginShellEnvInput = NonNullable<Parameters<typeof inheritLoginShellEnv>[0]>;
type LoginShellSpawn = NonNullable<LoginShellEnvInput["spawn"]>;

interface TestClock {
  advance: (ms: number) => void;
  now: () => number;
}

interface RecordedLog {
  message: string;
  fields: Record<string, unknown>;
}

interface RecordedSpawn {
  shell: string;
  args: string[];
  argv0: string | undefined;
}

class RecordingLoginShellLogger {
  readonly infos: RecordedLog[] = [];
  readonly warnings: RecordedLog[] = [];

  info(message: string, fields: Record<string, unknown>): void {
    this.infos.push({ message, fields });
  }

  warn(message: string, fields: Record<string, unknown>): void {
    this.warnings.push({ message, fields });
  }
}

class FakeChild extends ChildProcess {
  override stdout = new PassThrough();
  override stderr = new PassThrough();
  readonly killed: string[] = [];

  override kill(signal?: NodeJS.Signals | number): boolean {
    this.killed.push(String(signal));
    return true;
  }

  override unref(): void {}
}

function createEnv(home: string): NodeJS.ProcessEnv {
  return {
    HOME: home,
    USER: "paseo-test",
    LOGNAME: "paseo-test",
    SHELL: zsh,
    PATH: basePath,
  };
}

function createTestClock(): TestClock {
  let currentMs = 1_000;
  return {
    advance: (ms: number) => {
      currentMs += ms;
    },
    now: () => currentMs,
  };
}

type SpawnBehavior = (child: FakeChild, call: RecordedSpawn) => void;

function createFakeSpawn(behaviors: SpawnBehavior[]): {
  calls: RecordedSpawn[];
  children: FakeChild[];
  spawn: LoginShellSpawn;
} {
  const calls: RecordedSpawn[] = [];
  const children: FakeChild[] = [];
  const spawn: LoginShellSpawn = (_shell, args, options) => {
    const child = new FakeChild();
    const recordedArgs = Array.isArray(args) ? args.map(String) : [];
    const call: RecordedSpawn = {
      shell: String(_shell),
      args: recordedArgs,
      argv0: options?.argv0,
    };
    calls.push(call);
    children.push(child);
    const behavior = behaviors[Math.min(calls.length - 1, behaviors.length - 1)];
    queueMicrotask(() => behavior?.(child, call));
    return child;
  };
  return { calls, children, spawn };
}

function succeedWith(env: NodeJS.ProcessEnv, clock: TestClock, advanceMs: number): SpawnBehavior {
  return (child, call) => {
    clock.advance(advanceMs);
    const marker = markerFromShellCommand(String(call.args.at(-1)));
    child.stdout?.emit("data", `${marker}${JSON.stringify({ ...env, PATH: env.PATH })}${marker}`);
    child.emit("close", 0, null);
  };
}

function shellArgsFromRecordedCall(call: RecordedSpawn | undefined): string[] {
  if (!call) return [];
  return call.args.slice(0, -1);
}

function markerFromShellCommand(shellCommand: string): string {
  const match = /"([0-9a-f]{12})" \+ JSON\.stringify\(process\.env\) \+ "\1"/.exec(shellCommand);
  if (!match?.[1]) throw new Error(`missing env marker in shell command: ${shellCommand}`);
  return match[1];
}

function expectNoRawStdout(fields: Record<string, unknown>): void {
  expect(fields).not.toHaveProperty("stdout");
}

async function createShellHome(): Promise<string> {
  return await mkdtemp(path.join(os.tmpdir(), "paseo-login-shell-env-"));
}

describe("login shell env retry behavior", () => {
  it("preserves UTF-8 environment values split across output chunks", async () => {
    const env = createEnv(fakeHome);
    const logger = new RecordingLoginShellLogger();
    const { spawn } = createFakeSpawn([
      (child, call) => {
        const marker = markerFromShellCommand(String(call.args.at(-1)));
        const bytes = Buffer.from(`${marker}${JSON.stringify({ PATH: "/café/bin" })}${marker}`);
        const split = bytes.indexOf(0xc3) + 1;
        child.stdout.write(bytes.subarray(0, split));
        child.stdout.write(bytes.subarray(split));
        child.emit("close", 0, null);
      },
    ]);
    await inheritLoginShellEnv({ env, logger, spawn });
    expect(env.PATH).toBe("/café/bin");
  });

  it("applies the interactive env without retrying", async () => {
    const env = createEnv(fakeHome);
    const logger = new RecordingLoginShellLogger();
    const clock = createTestClock();
    const interactivePath = "/interactive/bin:/usr/bin:/bin";
    const { calls, spawn } = createFakeSpawn([
      succeedWith({ ...env, PATH: interactivePath }, clock, 5),
    ]);

    await inheritLoginShellEnv({ env, logger, now: clock.now, platform: "darwin", spawn });

    expect(env.PATH).toBe(interactivePath);
    expect(calls).toHaveLength(1);
    expect(shellArgsFromRecordedCall(calls[0])).toEqual(["-i", "-l", "-c"]);
    expect(logger.infos.map((entry) => entry.message)).toEqual([
      "[login-shell-env] start",
      "[login-shell-env] attempt applied",
      "[login-shell-env] applied",
    ]);
    expect(logger.infos[1]?.fields).toMatchObject({
      attemptKind: "interactive",
      shellArgs: ["-i", "-l", "-c"],
      reason: "success",
      timeoutMs: 15_000,
    });
    expect(logger.infos[2]?.fields).toMatchObject({
      attemptKind: "interactive",
      durationMs: 5,
      timeoutMs: 30_000,
      beforePath: basePath,
      afterPath: interactivePath,
      pathChanged: true,
    });
    expect(logger.warnings).toEqual([]);
  });

  it("retries non-interactively after an interactive timeout", async () => {
    const env = createEnv(fakeHome);
    const logger = new RecordingLoginShellLogger();
    const clock = createTestClock();
    const nonInteractivePath = "/login/bin:/usr/bin:/bin";
    const timeoutError = Object.assign(new Error("spawn ETIMEDOUT"), {
      code: "ETIMEDOUT",
    });
    let timedOutStdout = "";
    const { calls, spawn } = createFakeSpawn([
      (child, call) => {
        const marker = markerFromShellCommand(String(call.args.at(-1)));
        timedOutStdout = `${marker}${JSON.stringify({ ...env, PATH: "/timed-out/bin" })}${marker}`;
        clock.advance(15_000);
        child.stdout?.emit("data", timedOutStdout);
        child.emit("error", timeoutError);
      },
      succeedWith({ ...env, PATH: nonInteractivePath }, clock, 3),
    ]);

    await inheritLoginShellEnv({ env, logger, now: clock.now, platform: "darwin", spawn });

    expect(env.PATH).toBe(nonInteractivePath);
    expect(calls).toHaveLength(2);
    expect(shellArgsFromRecordedCall(calls[0])).toEqual(["-i", "-l", "-c"]);
    expect(shellArgsFromRecordedCall(calls[1])).toEqual(["-l", "-c"]);
    expect(logger.warnings).toHaveLength(1);
    expect(logger.warnings[0]?.message).toBe("[login-shell-env] attempt failed; retrying");
    expect(logger.warnings[0]?.fields).toMatchObject({
      reason: "timeout",
      attemptKind: "interactive",
      shellArgs: ["-i", "-l", "-c"],
      status: null,
      signal: null,
      stdoutLength: timedOutStdout.length,
      markerFound: true,
      errorCode: "ETIMEDOUT",
      durationMs: 15_000,
      timeoutMs: 15_000,
    });
    expect(logger.infos[1]?.fields).toMatchObject({
      attemptKind: "non-interactive",
      shellArgs: ["-l", "-c"],
      reason: "success",
      durationMs: 3,
      timeoutMs: 15_000,
    });
    expect(logger.infos[2]?.fields).toMatchObject({
      attemptKind: "non-interactive",
      durationMs: 15_003,
      timeoutMs: 30_000,
      beforePath: basePath,
      afterPath: nonInteractivePath,
      pathChanged: true,
    });
    expectNoRawStdout(logger.warnings[0]?.fields ?? {});
  });

  it("retries non-interactively when the interactive marker is missing", async () => {
    const env = createEnv(fakeHome);
    const logger = new RecordingLoginShellLogger();
    const clock = createTestClock();
    const nonInteractivePath = "/profile/bin:/usr/bin:/bin";
    const missingMarkerStdout = "switched shells before command\n";
    const { calls, spawn } = createFakeSpawn([
      (child) => {
        clock.advance(25);
        child.stdout?.emit("data", missingMarkerStdout);
        child.emit("close", 0, null);
      },
      succeedWith({ ...env, PATH: nonInteractivePath }, clock, 2),
    ]);

    await inheritLoginShellEnv({ env, logger, now: clock.now, platform: "darwin", spawn });

    expect(env.PATH).toBe(nonInteractivePath);
    expect(calls).toHaveLength(2);
    expect(shellArgsFromRecordedCall(calls[0])).toEqual(["-i", "-l", "-c"]);
    expect(shellArgsFromRecordedCall(calls[1])).toEqual(["-l", "-c"]);
    expect(logger.warnings).toHaveLength(1);
    expect(logger.warnings[0]?.message).toBe("[login-shell-env] attempt failed; retrying");
    expect(logger.warnings[0]?.fields).toMatchObject({
      reason: "marker-missing",
      attemptKind: "interactive",
      shellArgs: ["-i", "-l", "-c"],
      status: 0,
      signal: null,
      stdoutLength: missingMarkerStdout.length,
      markerFound: false,
      durationMs: 25,
      timeoutMs: 15_000,
    });
    expect(logger.infos[1]?.fields).toMatchObject({
      attemptKind: "non-interactive",
      reason: "success",
      durationMs: 2,
      timeoutMs: 29_975,
    });
    expectNoRawStdout(logger.warnings[0]?.fields ?? {});
  });

  it("keeps the inherited env after both attempts fail", async () => {
    const env = createEnv(fakeHome);
    const logger = new RecordingLoginShellLogger();
    const clock = createTestClock();
    const spawnError = Object.assign(new Error("spawn ENOENT"), {
      code: "ENOENT",
    });
    const { calls, spawn } = createFakeSpawn([
      (child) => {
        clock.advance(10);
        child.stdout?.emit("data", "no marker\n");
        child.emit("close", 0, null);
      },
      (child) => {
        clock.advance(5);
        child.emit("error", spawnError);
      },
    ]);

    await inheritLoginShellEnv({ env, logger, now: clock.now, platform: "darwin", spawn });

    expect(env.PATH).toBe(basePath);
    expect(calls).toHaveLength(2);
    expect(logger.infos.map((entry) => entry.message)).toEqual(["[login-shell-env] start"]);
    expect(logger.warnings.map((entry) => entry.message)).toEqual([
      "[login-shell-env] attempt failed; retrying",
      "[login-shell-env] attempt failed",
      "[login-shell-env] failed; keeping inherited env",
    ]);
    expect(logger.warnings[0]?.fields).toMatchObject({
      reason: "marker-missing",
      attemptKind: "interactive",
      shellArgs: ["-i", "-l", "-c"],
      durationMs: 10,
      timeoutMs: 15_000,
    });
    expect(logger.warnings[1]?.fields).toMatchObject({
      reason: "spawn-error",
      attemptKind: "non-interactive",
      shellArgs: ["-l", "-c"],
      durationMs: 5,
      errorCode: "ENOENT",
      timeoutMs: 29_990,
    });
    expect(logger.warnings[2]?.fields).toMatchObject({
      reason: "spawn-error",
      attemptKind: "non-interactive",
      shellArgs: ["-l", "-c"],
      errorCode: "ENOENT",
      durationMs: 15,
      timeoutMs: 30_000,
      beforePath: basePath,
      afterPath: basePath,
      pathChanged: false,
    });
    expectNoRawStdout(logger.warnings[2]?.fields ?? {});
  });

  it("uses the configured shell env timeout", async () => {
    const env = {
      ...createEnv(fakeHome),
      PASEO_SHELL_ENV_TIMEOUT_MS: "1234",
    };
    const logger = new RecordingLoginShellLogger();
    const clock = createTestClock();
    const configuredPath = "/configured/bin:/usr/bin:/bin";
    const { calls, spawn } = createFakeSpawn([
      succeedWith({ ...env, PATH: configuredPath }, clock, 4),
    ]);

    await inheritLoginShellEnv({ env, logger, now: clock.now, platform: "darwin", spawn });

    expect(env.PATH).toBe(configuredPath);
    expect(calls).toHaveLength(1);
    expect(logger.infos[0]?.fields).toMatchObject({
      timeoutMs: 1234,
    });
    expect(logger.infos[1]?.fields).toMatchObject({
      timeoutMs: 617,
    });
    expect(logger.infos[2]?.fields).toMatchObject({
      durationMs: 4,
      timeoutMs: 1234,
    });
  });

  it("uses argv0 for the non-interactive tcsh login retry", async () => {
    const env = {
      ...createEnv(fakeHome),
      SHELL: "/bin/tcsh",
    };
    const logger = new RecordingLoginShellLogger();
    const clock = createTestClock();
    const nonInteractivePath = "/tcsh/login/bin:/usr/bin:/bin";
    const { calls, spawn } = createFakeSpawn([
      (child) => {
        clock.advance(8);
        child.stdout?.emit("data", "no marker\n");
        child.emit("close", 0, null);
      },
      succeedWith({ ...env, PATH: nonInteractivePath }, clock, 2),
    ]);

    await inheritLoginShellEnv({ env, logger, now: clock.now, platform: "darwin", spawn });

    expect(env.PATH).toBe(nonInteractivePath);
    expect(calls).toHaveLength(2);
    expect(shellArgsFromRecordedCall(calls[0])).toEqual(["-ic"]);
    expect(calls[0]?.argv0).toBeUndefined();
    expect(shellArgsFromRecordedCall(calls[1])).toEqual(["-c"]);
    expect(calls[1]?.argv0).toBe("-tcsh");
    expect(logger.warnings[0]?.fields).toMatchObject({
      attemptKind: "interactive",
      shellArgs: ["-ic"],
      reason: "marker-missing",
      timeoutMs: 15_000,
    });
    expect(logger.infos[1]?.fields).toMatchObject({
      attemptKind: "non-interactive",
      argv0: "-tcsh",
      shellArgs: ["-c"],
      reason: "success",
      timeoutMs: 29_992,
    });
  });
});

describeIfZsh("login shell env", () => {
  const homes = new Set<string>();

  afterEach(async () => {
    await Promise.all([...homes].map((home) => rm(home, { recursive: true, force: true })));
    homes.clear();
  });

  it("applies PATH from the user's login shell", async () => {
    const home = await createShellHome();
    homes.add(home);
    const binDir = path.join(home, "tools");
    await mkdir(binDir);
    await writeFile(path.join(home, ".zprofile"), 'export PATH="$HOME/tools:$PATH"\n');
    const env = createEnv(home);
    const logger = new RecordingLoginShellLogger();

    await inheritLoginShellEnv({ env, logger });

    expect(env.PATH?.split(path.delimiter)[0]).toBe(binDir);
    expect(logger.infos.map((entry) => entry.message)).toEqual([
      "[login-shell-env] start",
      "[login-shell-env] attempt applied",
      "[login-shell-env] applied",
    ]);
    expect(logger.warnings).toEqual([]);
    expect(logger.infos[1]?.fields).toMatchObject({
      attemptKind: "interactive",
      shellArgs: ["-i", "-l", "-c"],
      reason: "success",
    });
    expect(logger.infos[2]?.fields).toMatchObject({
      attemptKind: "interactive",
      beforePath: basePath,
      afterPath: env.PATH,
      pathChanged: true,
      shell: zsh,
    });
  });

  it("loads the user's zshrc while resolving the login shell env", async () => {
    const home = await createShellHome();
    homes.add(home);
    await writeFile(path.join(home, ".zshrc"), "export PASEO_TEST_ZSHRC_LOADED=1\n");
    const env = createEnv(home);
    const logger = new RecordingLoginShellLogger();

    await inheritLoginShellEnv({ env, logger });

    expect(env.PASEO_TEST_ZSHRC_LOADED).toBe("1");
    expect(logger.infos.map((entry) => entry.message)).toEqual([
      "[login-shell-env] start",
      "[login-shell-env] attempt applied",
      "[login-shell-env] applied",
    ]);
    expect(logger.warnings).toEqual([]);
  });

  it("keeps the inherited env and logs stdout diagnostics when shell startup fails", async () => {
    const home = await createShellHome();
    homes.add(home);
    await writeFile(path.join(home, ".zshenv"), "print -r -- premarker\nexit 42\n");
    const env = createEnv(home);
    const logger = new RecordingLoginShellLogger();

    await inheritLoginShellEnv({ env, logger });

    expect(env.PATH).toBe(basePath);
    expect(logger.infos.map((entry) => entry.message)).toEqual(["[login-shell-env] start"]);
    expect(logger.warnings.map((entry) => entry.message)).toEqual([
      "[login-shell-env] attempt failed; retrying",
      "[login-shell-env] attempt failed",
      "[login-shell-env] failed; keeping inherited env",
    ]);
    expect(logger.warnings[0]?.fields).toMatchObject({
      reason: "non-zero-exit",
      attemptKind: "interactive",
      shell: zsh,
      shellArgs: ["-i", "-l", "-c"],
      status: 42,
      stdoutLength: "premarker\n".length,
      markerFound: false,
    });
    expect(logger.warnings[1]?.fields).toMatchObject({
      reason: "non-zero-exit",
      attemptKind: "non-interactive",
      shell: zsh,
      shellArgs: ["-l", "-c"],
      status: 42,
      stdoutLength: "premarker\n".length,
      markerFound: false,
    });
    expect(logger.warnings[2]?.fields).toMatchObject({
      reason: "non-zero-exit",
      attemptKind: "non-interactive",
      shell: zsh,
      shellArgs: ["-l", "-c"],
      status: 42,
      stdoutLength: "premarker\n".length,
      markerFound: false,
      beforePath: basePath,
      afterPath: basePath,
      pathChanged: false,
    });
    expectNoRawStdout(logger.warnings[2]?.fields ?? {});
  });

  it("keeps the inherited env when a timed-out shell printed an env marker", async () => {
    const home = await createShellHome();
    homes.add(home);
    const env = createEnv(home);
    const logger = new RecordingLoginShellLogger();
    const timedOutPath = path.join(home, "timed-out");
    let stdout = "";
    const timeoutError = Object.assign(new Error("spawn ETIMEDOUT"), {
      code: "ETIMEDOUT",
    });
    const failWithTimedOutMarker: SpawnBehavior = (child, call) => {
      const shellCommand = String(call.args.at(-1) ?? "");
      const marker = markerFromShellCommand(shellCommand);
      stdout = `${marker}${JSON.stringify({ ...env, PATH: timedOutPath })}${marker}`;
      child.stdout?.emit("data", stdout);
      child.emit("error", timeoutError);
    };
    const { spawn } = createFakeSpawn([failWithTimedOutMarker, failWithTimedOutMarker]);

    await inheritLoginShellEnv({ env, logger, spawn });

    expect(env.PATH).toBe(basePath);
    expect(logger.infos.map((entry) => entry.message)).toEqual(["[login-shell-env] start"]);
    expect(logger.warnings.map((entry) => entry.message)).toEqual([
      "[login-shell-env] attempt failed; retrying",
      "[login-shell-env] attempt failed",
      "[login-shell-env] failed; keeping inherited env",
    ]);
    expect(logger.warnings[0]?.fields).toMatchObject({
      reason: "timeout",
      attemptKind: "interactive",
      shell: zsh,
      shellArgs: ["-i", "-l", "-c"],
      status: null,
      signal: null,
      stdoutLength: stdout.length,
      markerFound: true,
      errorCode: "ETIMEDOUT",
    });
    expect(logger.warnings[1]?.fields).toMatchObject({
      reason: "timeout",
      attemptKind: "non-interactive",
      shell: zsh,
      shellArgs: ["-l", "-c"],
      status: null,
      signal: null,
      stdoutLength: stdout.length,
      markerFound: true,
      errorCode: "ETIMEDOUT",
    });
    expect(logger.warnings[2]?.fields).toMatchObject({
      reason: "timeout",
      attemptKind: "non-interactive",
      shell: zsh,
      shellArgs: ["-l", "-c"],
      status: null,
      signal: null,
      stdoutLength: stdout.length,
      markerFound: true,
      errorCode: "ETIMEDOUT",
      beforePath: basePath,
      afterPath: basePath,
      pathChanged: false,
    });
    expectNoRawStdout(logger.warnings[2]?.fields ?? {});
  });
});

describeIfPosix("login shell env hang regression", () => {
  it("keeps the event loop responsive and kills TERM-ignoring descendants within the deadline", async () => {
    const home = await createShellHome();
    const pidFile = path.join(home, "owned.pids");
    const wrapper = path.join(home, "paseo-hang-shell");
    await writeFile(
      wrapper,
      [
        "#!/bin/sh",
        "trap '' TERM",
        `echo $$ >> "${pidFile}"`,
        "sleep 45 &",
        `echo $! >> "${pidFile}"`,
        "wait",
        "",
      ].join("\n"),
    );
    await chmod(wrapper, 0o700);
    const env = { PATH: basePath, HOME: home, SHELL: wrapper, PASEO_SHELL_ENV_TIMEOUT_MS: "600" };
    const logger = new RecordingLoginShellLogger();
    let beats = 0;
    const heartbeat = setInterval(() => {
      beats += 1;
    }, 20);
    const startedAt = Date.now();
    try {
      await inheritLoginShellEnv({ env, logger });
      const elapsed = Date.now() - startedAt;
      const pids = (await readFile(pidFile, "utf8")).trim().split(/\s+/).map(Number);
      expect(new Set(pids).size).toBe(4);
      for (const pid of pids) {
        await expect.poll(() => pidRunning(pid), { timeout: 2000 }).toBe(false);
      }
      expect(elapsed).toBeLessThan(1800);
      expect(beats).toBeGreaterThan(5);
      expect(env.PATH).toBe(basePath);
      expect(logger.warnings.filter((entry) => entry.fields.reason === "timeout")).toHaveLength(3);
    } finally {
      clearInterval(heartbeat);
      if (existsSync(pidFile)) {
        const pids = (await readFile(pidFile, "utf8")).trim().split(/\s+/).map(Number);
        for (const pid of pids) {
          if (await pidRunning(pid)) process.kill(pid, "SIGKILL");
        }
      }
      await rm(home, { recursive: true, force: true });
    }
  });
});

async function pidRunning(pid: number): Promise<boolean> {
  try {
    if (process.platform === "linux") {
      const stat = await readFile(`/proc/${pid}/stat`, "utf8");
      return stat.slice(stat.lastIndexOf(")") + 2, stat.lastIndexOf(")") + 3) !== "Z";
    }
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (
      error instanceof Error &&
      "code" in error &&
      ["ENOENT", "ESRCH"].includes(String(error.code))
    )
      return false;
    throw error;
  }
}
