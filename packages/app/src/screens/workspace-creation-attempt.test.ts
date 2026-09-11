import { describe, expect, it, vi } from "vitest";
import {
  createWorkspaceCreationAttempt,
  retryWorkspaceCreationAttempt,
  runWorkspaceCreationAttempt,
} from "./workspace-creation-attempt";

function rejectAfterClientDeadline(): Promise<never> {
  return new Promise((_resolve, reject) =>
    setTimeout(() => reject(new Error("request timed out")), 60_000),
  );
}

describe("frozen workspace creation attempt", () => {
  it("keeps the target and initial prompt when the form input later changes", () => {
    const input = {
      source: { kind: "directory" as const, path: "/original" },
      firstAgentContext: { prompt: "original", attachments: [] },
    };
    const attempt = createWorkspaceCreationAttempt(input);
    input.source.path = "/changed";
    input.firstAgentContext.prompt = "changed";
    expect(attempt.source).toEqual({ kind: "directory", path: "/original" });
    expect(attempt.firstAgentContext?.prompt).toBe("original");
  });

  it("retains an uncertain attempt and retries with the exact same ID and input", async () => {
    const attempt = createWorkspaceCreationAttempt({
      source: { kind: "directory", path: "/repo" },
    });
    const createWorkspace = vi
      .fn()
      .mockRejectedValueOnce(new Error("Request timed out after 60000ms"))
      .mockResolvedValue({ workspace: { id: "wks_existing" }, error: null });
    const client = {
      createWorkspace,
      supportsWorkspaceCreationRetry: () => true,
      requireWorkspaceCreationRetrySupport: vi.fn(),
    };
    await expect(runWorkspaceCreationAttempt({ client, attempt })).rejects.toMatchObject({
      kind: "timeout",
      retryable: true,
    });
    await expect(retryWorkspaceCreationAttempt({ client, attempt })).resolves.toMatchObject({
      workspaceId: "wks_existing",
    });
    expect(createWorkspace.mock.calls[1]).toEqual(createWorkspace.mock.calls[0]);
    expect(createWorkspace.mock.calls[1]?.[1]).toBe(attempt.requestId);
  });

  it("does not send a retry to an old host or discard its unresolved identity", async () => {
    const attempt = createWorkspaceCreationAttempt({
      source: { kind: "directory", path: "/repo" },
    });
    const client = {
      createWorkspace: vi.fn(),
      supportsWorkspaceCreationRetry: () => false,
      requireWorkspaceCreationRetrySupport: vi.fn(),
    };
    await expect(retryWorkspaceCreationAttempt({ client, attempt })).rejects.toMatchObject({
      kind: "host-update-required",
      retryable: true,
    });
    expect(client.createWorkspace).not.toHaveBeenCalled();
  });

  it("retains server failures with unknown completion but rejects archived or mismatched attempts", async () => {
    const attempt = createWorkspaceCreationAttempt({
      source: { kind: "directory", path: "/repo" },
    });
    const createWorkspace = vi
      .fn()
      .mockResolvedValueOnce({ workspace: null, error: "notification failed" })
      .mockResolvedValueOnce({
        workspace: null,
        error: "archived",
        errorCode: "creation_attempt_archived",
      })
      .mockResolvedValueOnce({
        workspace: null,
        error: "changed",
        errorCode: "creation_request_mismatch",
      });
    const client = {
      createWorkspace,
      supportsWorkspaceCreationRetry: () => true,
      requireWorkspaceCreationRetrySupport: vi.fn(),
    };
    await expect(runWorkspaceCreationAttempt({ client, attempt })).rejects.toMatchObject({
      retryable: true,
    });
    await expect(runWorkspaceCreationAttempt({ client, attempt })).rejects.toMatchObject({
      kind: "archived",
      retryable: false,
    });
    await expect(runWorkspaceCreationAttempt({ client, attempt })).rejects.toMatchObject({
      kind: "mismatch",
      retryable: false,
    });
  });
  it("reconciles a creation that finishes after the 60-second client deadline", async () => {
    vi.useFakeTimers();
    try {
      const attempt = createWorkspaceCreationAttempt({
        source: { kind: "directory", path: "/repo" },
      });
      const operation = new Promise<{ workspace: { id: string }; error: null }>((resolve) =>
        setTimeout(() => resolve({ workspace: { id: "late-workspace" }, error: null }), 90_000),
      );
      const createWorkspace = vi.fn((_input, requestId: string) => {
        expect(requestId).toBe(attempt.requestId);
        return Promise.race([operation, rejectAfterClientDeadline()]);
      });
      const client = {
        createWorkspace,
        supportsWorkspaceCreationRetry: () => true,
        requireWorkspaceCreationRetrySupport: () => {},
      };
      const first = expect(runWorkspaceCreationAttempt({ client, attempt })).rejects.toMatchObject({
        kind: "timeout",
        retryable: true,
      });
      await vi.advanceTimersByTimeAsync(60_001);
      await first;
      const retry = retryWorkspaceCreationAttempt({ client, attempt });
      await vi.advanceTimersByTimeAsync(30_000);
      await expect(retry).resolves.toMatchObject({ workspaceId: "late-workspace" });
      expect(createWorkspace.mock.calls[1]).toEqual(createWorkspace.mock.calls[0]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("never replays an uncertain legacy dispatch, even after the host is upgraded", async () => {
    const attempt = createWorkspaceCreationAttempt({
      source: { kind: "worktree", cwd: "/repo", worktreeSlug: "frozen" },
    });
    let upgraded = false;
    const client = {
      createWorkspace: vi.fn(),
      supportsWorkspaceCreationRetry: () => upgraded,
      requireWorkspaceCreationRetrySupport: vi.fn(),
    };
    const legacyCreate = vi.fn(async () => {
      throw new Error("request timed out");
    });
    await expect(
      runWorkspaceCreationAttempt({ client, attempt, legacyCreate }),
    ).rejects.toMatchObject({ retryable: true });
    expect(legacyCreate).toHaveBeenCalledWith(attempt.requestId);
    upgraded = true;
    await expect(retryWorkspaceCreationAttempt({ client, attempt })).rejects.toMatchObject({
      kind: "host-update-required",
      retryable: true,
    });
    expect(legacyCreate).toHaveBeenCalledOnce();
    expect(client.createWorkspace).not.toHaveBeenCalled();
  });
});
