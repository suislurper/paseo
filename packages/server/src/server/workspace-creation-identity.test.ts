import { describe, expect, it } from "vitest";
import {
  computeWorkspaceCreationFingerprint,
  runWorkspaceCreationIdempotent,
} from "./workspace-creation-identity.js";

describe("workspace creation identity", () => {
  it("coalesces the same request across sessions and preserves different intentional IDs", async () => {
    const owner = {};
    let complete!: () => void;
    const pending = new Promise<void>((resolve) => {
      complete = resolve;
    });
    let calls = 0;
    const runner = async () => {
      calls++;
      await pending;
      return { workspace: null, error: null, archived: false, reconciled: false };
    };
    const first = runWorkspaceCreationIdempotent({
      owner,
      requestId: "first",
      fingerprint: "same",
      runner,
    });
    const retry = runWorkspaceCreationIdempotent({
      owner,
      requestId: "first",
      fingerprint: "same",
      runner,
    });
    const separate = runWorkspaceCreationIdempotent({
      owner,
      requestId: "second",
      fingerprint: "same",
      runner,
    });
    expect(calls).toBe(2);
    await expect(
      runWorkspaceCreationIdempotent({ owner, requestId: "first", fingerprint: "changed", runner }),
    ).rejects.toThrow("different input");
    complete();
    expect((await first).reconciled).toBe(false);
    expect((await retry).reconciled).toBe(true);
    expect((await separate).reconciled).toBe(false);
  });

  it("fingerprints the frozen target, title and initial context independent of object key order", () => {
    const input = {
      requestId: "same",
      title: "Task",
      source: { kind: "worktree" as const, cwd: "/repo", worktreeSlug: "one" },
      firstAgentContext: { prompt: "original", attachments: [] },
    };
    const fingerprint = computeWorkspaceCreationFingerprint(input);
    expect(
      computeWorkspaceCreationFingerprint({
        ...input,
        source: { worktreeSlug: "one", cwd: "/repo", kind: "worktree" },
      }),
    ).toBe(fingerprint);
    expect(
      computeWorkspaceCreationFingerprint({
        ...input,
        source: { ...input.source, worktreeSlug: "two" },
      }),
    ).not.toBe(fingerprint);
    expect(
      computeWorkspaceCreationFingerprint({
        ...input,
        firstAgentContext: { prompt: "changed", attachments: [] },
      }),
    ).not.toBe(fingerprint);
  });
});
