import { describe, expect, it } from "vitest";
import { parseDuration } from "./duration.js";

describe("parseDuration", () => {
  it("parses milliseconds and compound durations", () => {
    expect(parseDuration("500ms")).toBe(500);
    expect(parseDuration("2h30m")).toBe(9_000_000);
  });

  it("treats a bare number as seconds", () => {
    expect(parseDuration("90")).toBe(90_000);
  });

  it.each(["500milliseconds", "5m trailing", "prefix5m", "5m30"])(
    "rejects partially consumed input %s",
    (input) => {
      expect(() => parseDuration(input)).toThrow("Invalid duration format");
    },
  );
});
