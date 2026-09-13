import { describe, expect, it, vi } from "vitest";
import { runDesktopStartup } from "./desktop-startup";

describe("desktop startup", () => {
  it("runs CLI passthrough before GUI login-shell env inheritance", async () => {
    const calls: string[] = [];
    await runDesktopStartup({
      hasPendingOpenProjectPath: false,
      runCliPassthroughIfRequested: vi.fn(async () => {
        calls.push("cli");
        return true;
      }),
      inheritLoginShellEnv: vi.fn(async () => {
        calls.push("env");
      }),
      bootstrapGui: vi.fn(async () => {
        calls.push("gui");
      }),
    });

    expect(calls).toEqual(["cli"]);
  });

  it("keeps login-shell env inheritance on normal GUI startup", async () => {
    const calls: string[] = [];
    await runDesktopStartup({
      hasPendingOpenProjectPath: false,
      runCliPassthroughIfRequested: vi.fn(async () => {
        calls.push("cli");
        return false;
      }),
      inheritLoginShellEnv: vi.fn(async () => {
        calls.push("env");
      }),
      bootstrapGui: vi.fn(async () => {
        calls.push("gui");
      }),
    });

    expect(calls).toEqual(["cli", "env", "gui"]);
  });

  it("starts skills auto-update after GUI startup", async () => {
    const calls: string[] = [];
    await runDesktopStartup({
      hasPendingOpenProjectPath: false,
      runCliPassthroughIfRequested: vi.fn(async () => {
        calls.push("cli");
        return false;
      }),
      inheritLoginShellEnv: vi.fn(async () => {
        calls.push("env");
      }),
      bootstrapGui: vi.fn(async () => {
        calls.push("gui");
      }),
      autoUpdateInstalledSkills: vi.fn(() => calls.push("skills")),
    });

    expect(calls).toEqual(["cli", "env", "gui", "skills"]);
  });

  it("does not route open-project launches through CLI passthrough", async () => {
    const runCliPassthroughIfRequested = vi.fn(async () => true);
    const calls: string[] = [];
    const inheritLoginShellEnv = vi.fn(async () => {
      calls.push("env");
    });

    await runDesktopStartup({
      hasPendingOpenProjectPath: true,
      runCliPassthroughIfRequested,
      inheritLoginShellEnv,
      bootstrapGui: vi.fn(async () => {
        calls.push("gui");
      }),
    });

    expect(runCliPassthroughIfRequested).not.toHaveBeenCalled();
    expect(inheritLoginShellEnv).toHaveBeenCalledTimes(1);
    expect(calls).toEqual(["env", "gui"]);
  });

  it("waits for login-shell env resolution before bootstrap", async () => {
    const calls: string[] = [];
    let releaseEnv!: () => void;
    const envGate = new Promise<void>((resolve) => {
      releaseEnv = resolve;
    });
    const startup = runDesktopStartup({
      hasPendingOpenProjectPath: false,
      runCliPassthroughIfRequested: vi.fn(async () => {
        calls.push("cli");
        return false;
      }),
      inheritLoginShellEnv: vi.fn(async () => {
        await envGate;
        calls.push("env");
      }),
      bootstrapGui: vi.fn(async () => {
        calls.push("gui");
      }),
    });

    await Promise.resolve();
    await Promise.resolve();
    expect(calls).toEqual(["cli"]);
    releaseEnv();
    await startup;

    expect(calls).toEqual(["cli", "env", "gui"]);
  });
});
