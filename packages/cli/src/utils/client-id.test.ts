import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { getCliClientId } from "./client-id.js";

describe("getCliClientId", () => {
  it("returns the same identity within one invocation", () => {
    expect(getCliClientId()).toBe(getCliClientId());
  });

  it("isolates independent invocations without writing a shared identity", () => {
    const home = mkdtempSync(join(tmpdir(), "paseo-cli-identity-"));
    try {
      const source = new URL("./client-id.ts", import.meta.url).href;
      const script = `import { getCliClientId } from ${JSON.stringify(source)}; console.log(getCliClientId()); console.log(getCliClientId());`;
      const invoke = () =>
        execFileSync(process.execPath, ["--import", "tsx", "--input-type=module", "-e", script], {
          encoding: "utf8",
          env: { PATH: process.env.PATH, HOME: home, PASEO_HOME: join(home, "unused") },
        })
          .trim()
          .split("\n");
      const first = invoke();
      const second = invoke();
      expect(first[0]).toBe(first[1]);
      expect(second[0]).toBe(second[1]);
      expect(first[0]).not.toBe(second[0]);
      expect(existsSync(join(home, "unused"))).toBe(false);
    } finally {
      rmSync(home, { recursive: true });
    }
  });
});
