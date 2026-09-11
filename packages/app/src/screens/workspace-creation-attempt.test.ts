import { describe, expect, it, vi } from "vitest";
import {
  createWorkspaceCreationAttempt,
  retryWorkspaceCreationAttempt,
  runWorkspaceCreationAttempt,
} from "./workspace-creation-attempt";

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
});
