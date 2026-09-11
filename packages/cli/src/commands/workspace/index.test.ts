import { describe, expect, it, vi } from "vitest";
import type { DaemonClient } from "@getpaseo/client/internal/daemon-client";
import { createWorkspaceCommand, runWorkspaceArchiveCommand } from "./index.js";

describe("workspace archive", () => {
  it("exposes record-only on the exact workspace command", () => {
    const command = createWorkspaceCommand().commands.find((entry) => entry.name() === "archive");
    expect(command?.options.some((option) => option.long === "--record-only")).toBe(true);
    expect(command?.registeredArguments[0]?.name()).toBe("workspace-id");
  });
  it.each([true, false])(
    "reports retained files and forwards recordOnly=%s",
    async (recordOnly) => {
      const archiveWorkspace = vi.fn(async () => ({
        requestId: "req",
        workspaceId: "wks_exact",
        archivedAt: "2026-09-11T00:00:00Z",
        error: null,
        cleanup: { status: "retained" as const, reason: "active reference" },
      }));
      const close = vi.fn(async () => {});
      const client = { archiveWorkspace, close } as unknown as DaemonClient;
      const result = await runWorkspaceArchiveCommand(
        "wks_exact",
        { recordOnly },
        { connectToDaemon: async () => client },
      );
      expect(archiveWorkspace).toHaveBeenCalledWith("wks_exact", undefined, {
        mode: recordOnly ? "archive_only" : "archive_and_cleanup",
      });
      expect(result.data).toMatchObject({
        workspaceId: "wks_exact",
        cleanup: "retained",
        reason: "active reference",
      });
      expect(close).toHaveBeenCalledOnce();
    },
  );
});
