import { describe, expect, it } from "vitest";
import { toTerminalRow } from "./schema.js";

describe("toTerminalRow", () => {
  it("retains workspaceId while keeping the cwd fallback", () => {
    expect(
      toTerminalRow({ id: "term-1", name: "shell", cwd: "/repo", workspaceId: "wks_abc" }),
    ).toEqual({ id: "term-1", name: "shell", cwd: "/repo", workspaceId: "wks_abc" });
  });

  it("falls back to the response cwd and omits missing workspaceId", () => {
    expect(toTerminalRow({ id: "term-2", name: "shell" }, "/fallback")).toEqual({
      id: "term-2",
      name: "shell",
      cwd: "/fallback",
    });
  });

  it("never invents a cwd for global terminal responses", () => {
    expect(toTerminalRow({ id: "term-3", name: "shell", workspaceId: "wks_abc" })).toEqual({
      id: "term-3",
      name: "shell",
      cwd: "-",
      workspaceId: "wks_abc",
    });
  });
});
