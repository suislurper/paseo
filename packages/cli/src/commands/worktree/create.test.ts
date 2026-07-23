import type { Command } from "commander";
import { describe, expect, it, vi } from "vitest";
import type { DaemonClient } from "@getpaseo/client/internal/daemon-client";
import { runCreateCommandWithDeps } from "./create.js";

function createFakeDaemonClient(
  setupStatuses: Array<{
    status: "running" | "completed" | "failed";
    error: string | null;
  } | null>,
): DaemonClient {
  let setupStatusIndex = 0;
  return {
    createPaseoWorktree: async () =>
      ({
        workspace: {
          id: "workspace-1",
          name: "feature-example",
          workspaceDirectory: "/tmp/worktrees/feature-example",
        },
        error: null,
      }) as Awaited<ReturnType<DaemonClient["createPaseoWorktree"]>>,
    fetchWorkspaceSetupStatus: async () => {
      const status = setupStatuses[Math.min(setupStatusIndex, setupStatuses.length - 1)] ?? null;
      setupStatusIndex += 1;
      return {
        requestId: `setup-${setupStatusIndex}`,
        workspaceId: "workspace-1",
        snapshot: status
          ? {
              ...status,
              detail: {
                type: "worktree_setup",
                worktreePath: "/tmp/worktrees/feature-example",
                branchName: "feature-example",
                log: "",
                commands: [],
              },
            }
          : null,
      };
    },
    close: async () => {},
  } as unknown as DaemonClient;
}

function createDependencies(client: DaemonClient, clock: { now: number }) {
  return {
    connectToDaemon: async () => client,
    now: () => clock.now,
    sleep: vi.fn(async (milliseconds: number) => {
      clock.now += milliseconds;
    }),
    settleWithin: async <T>(promise: Promise<T>) => ({
      timedOut: false as const,
      value: await promise,
    }),
  };
}

describe("runCreateCommandWithDeps", () => {
  it("returns immediately by default and exposes the authoritative workspace identity", async () => {
    const client = createFakeDaemonClient([]);
    const fetchStatus = vi.spyOn(client, "fetchWorkspaceSetupStatus");
    const dependencies = createDependencies(client, { now: 0 });

    const result = await runCreateCommandWithDeps(
      { mode: "branch-off", newBranch: "feature/example" },
      {} as Command,
      dependencies,
    );

    expect(fetchStatus).not.toHaveBeenCalled();
    expect(result).toMatchObject({
      type: "single",
      data: {
        workspaceId: "workspace-1",
        branchName: "feature-example",
        worktreePath: "/tmp/worktrees/feature-example",
      },
    });
  });

  it("waits through missing and running setup snapshots until setup completes", async () => {
    const client = createFakeDaemonClient([
      null,
      { status: "running", error: null },
      { status: "completed", error: null },
    ]);
    const dependencies = createDependencies(client, { now: 0 });

    const result = await runCreateCommandWithDeps(
      { mode: "branch-off", newBranch: "feature/example", wait: true },
      {} as Command,
      dependencies,
    );

    expect(dependencies.sleep).toHaveBeenCalledTimes(2);
    expect(result.type).toBe("single");
  });

  it("fails closed when setup reports failure", async () => {
    const client = createFakeDaemonClient([{ status: "failed", error: "pnpm install failed" }]);
    const dependencies = createDependencies(client, { now: 0 });

    await expect(
      runCreateCommandWithDeps(
        { mode: "branch-off", newBranch: "feature/example", wait: true },
        {} as Command,
        dependencies,
      ),
    ).rejects.toMatchObject({
      code: "WORKTREE_SETUP_FAILED",
      details: "pnpm install failed",
    });
  });

  it("times out while setup remains unavailable", async () => {
    const client = createFakeDaemonClient([]);
    const clock = { now: 0 };
    const dependencies = createDependencies(client, clock);

    await expect(
      runCreateCommandWithDeps(
        {
          mode: "branch-off",
          newBranch: "feature/example",
          wait: true,
          waitTimeout: "500ms",
        },
        {} as Command,
        dependencies,
      ),
    ).rejects.toMatchObject({
      code: "WORKTREE_SETUP_TIMEOUT",
    });
    expect(clock.now).toBe(500);
  });

  it("enforces the deadline when a setup-status request never resolves and closes the client", async () => {
    const client = createFakeDaemonClient([]);
    client.fetchWorkspaceSetupStatus = async () => new Promise(() => {});
    const close = vi.spyOn(client, "close");
    const dependencies = {
      ...createDependencies(client, { now: 0 }),
      settleWithin: vi.fn(async () => ({ timedOut: true as const })),
    };

    await expect(
      runCreateCommandWithDeps(
        {
          mode: "branch-off",
          newBranch: "feature/example",
          wait: true,
          waitTimeout: "5s",
        },
        {} as Command,
        dependencies,
      ),
    ).rejects.toMatchObject({
      code: "WORKTREE_SETUP_TIMEOUT",
    });
    expect(dependencies.settleWithin).toHaveBeenCalledWith(expect.any(Promise), 5_000);
    expect(close).toHaveBeenCalledTimes(1);
  });
});
