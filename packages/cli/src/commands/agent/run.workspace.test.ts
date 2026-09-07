import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { runRunCommand } from "./run.js";

const createWorkspace = vi.fn();
const createAgent = vi.fn();
const close = vi.fn(async () => undefined);

vi.mock("../../utils/client.js", () => ({
  connectToDaemon: vi.fn(async () => ({
    createWorkspace,
    createAgent,
    close,
  })),
  getDaemonHost: vi.fn(() => "localhost:6767"),
}));

describe("runRunCommand workspace resolution", () => {
  const originalWorkspaceId = process.env.PASEO_WORKSPACE_ID;

  beforeEach(() => {
    delete process.env.PASEO_WORKSPACE_ID;
    createWorkspace.mockReset();
    createAgent.mockReset();
    close.mockClear();
  });

  afterEach(() => {
    if (originalWorkspaceId === undefined) {
      delete process.env.PASEO_WORKSPACE_ID;
    } else {
      process.env.PASEO_WORKSPACE_ID = originalWorkspaceId;
    }
  });

  it("does not create a workspace before a bare run whose provider rejects", async () => {
    createAgent.mockRejectedValueOnce(new Error("provider rejected"));

    await expect(
      runRunCommand(
        "do something",
        { provider: "codex", cwd: "/tmp/run-cwd", detach: true },
        {} as never,
      ),
    ).rejects.toMatchObject({
      code: "AGENT_CREATE_FAILED",
      message: "Failed to create agent: provider rejected",
    });

    expect(createWorkspace).not.toHaveBeenCalled();
    expect(createAgent).toHaveBeenCalledTimes(1);
    expect(createAgent.mock.calls[0]?.[0]).toMatchObject({
      provider: "codex",
      cwd: "/tmp/run-cwd",
      initialPrompt: "do something",
      workspaceId: undefined,
    });
  });

  it.each([
    {
      label: "explicit --workspace",
      options: { workspace: "ws-explicit" },
      expectedId: "ws-explicit",
    },
    {
      label: "ambient PASEO_WORKSPACE_ID",
      options: {},
      env: "ws-ambient",
      expectedId: "ws-ambient",
    },
  ])("reuses $label without creating a workspace", async ({ options, env, expectedId }) => {
    if (env) {
      process.env.PASEO_WORKSPACE_ID = env;
    }
    createAgent.mockResolvedValueOnce({
      id: "agent-1",
      status: "idle",
      provider: "codex",
      cwd: "/tmp/run-cwd",
      workspaceId: expectedId,
      title: null,
    });

    const result = await runRunCommand(
      "do something",
      { provider: "codex", cwd: "/tmp/run-cwd", detach: true, ...options },
      {} as never,
    );

    expect(createWorkspace).not.toHaveBeenCalled();
    expect(createAgent).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceId: expectedId,
        cwd: "/tmp/run-cwd",
      }),
    );
    expect(result.data.agentId).toBe("agent-1");
  });

  it("still mints a worktree-backed workspace before createAgent", async () => {
    createWorkspace.mockResolvedValueOnce({
      workspace: {
        id: "ws-worktree",
        name: "feat",
        workspaceDirectory: "/tmp/wt",
        gitRuntime: { currentBranch: "feat" },
      },
    });
    createAgent.mockResolvedValueOnce({
      id: "agent-wt",
      status: "idle",
      provider: "codex",
      cwd: "/tmp/wt",
      workspaceId: "ws-worktree",
      title: null,
    });

    const result = await runRunCommand(
      "do something",
      { provider: "codex", cwd: "/tmp/run-cwd", worktree: "feat", detach: true },
      {} as never,
    );

    expect(createWorkspace).toHaveBeenCalledWith({
      source: {
        kind: "worktree",
        cwd: "/tmp/run-cwd",
        worktreeSlug: "feat",
        baseBranch: undefined,
      },
    });
    expect(createAgent).toHaveBeenCalledWith(
      expect.objectContaining({
        workspaceId: "ws-worktree",
        cwd: "/tmp/wt",
      }),
    );
    expect(result.data.agentId).toBe("agent-wt");
  });
});
