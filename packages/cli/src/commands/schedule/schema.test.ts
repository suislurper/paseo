import { describe, expect, test } from "vitest";
import {
  createScheduleIdentityInspectRows,
  createScheduleIdentityInspectSchema,
} from "./schema.js";
import type { ScheduleRecord } from "./types.js";

function scheduleWithHistory(): ScheduleRecord {
  return {
    id: "fb12e97c",
    name: "Release Captain audit wake",
    prompt: "secret wake prompt",
    cadence: { type: "every", everyMs: 300_000 },
    target: {
      type: "agent",
      agentId: "16519b37-1614-47f7-92db-01d0208b4529",
    },
    status: "active",
    createdAt: "2026-07-28T16:26:48.515Z",
    updatedAt: "2026-08-01T21:41:50.556Z",
    nextRunAt: "2026-08-01T21:51:48.515Z",
    lastRunAt: "2026-08-01T21:41:50.556Z",
    pausedAt: null,
    expiresAt: null,
    maxRuns: null,
    runs: Array.from({ length: 2_000 }, (_, index) => ({
      id: `run-${index}`,
      scheduledFor: "2026-08-01T21:41:48.515Z",
      startedAt: "2026-08-01T21:41:50.556Z",
      endedAt: "2026-08-01T21:42:00.000Z",
      status: "succeeded" as const,
      agentId: "16519b37-1614-47f7-92db-01d0208b4529",
      output: `large output ${index}`,
      error: null,
    })),
  };
}

describe("bounded schedule identity output", () => {
  test("keeps full target authority and omits prompt and run history", () => {
    const schedule = scheduleWithHistory();
    const rows = createScheduleIdentityInspectRows(schedule);
    const schema = createScheduleIdentityInspectSchema(schedule);
    const serialized = schema.serialize?.(rows[0]);

    expect(rows.map((row) => row.key)).not.toContain("Prompt");
    expect(rows.map((row) => row.key)).not.toContain("RunCount");
    expect(serialized).toEqual({
      id: "fb12e97c",
      name: "Release Captain audit wake",
      cadence: { type: "every", everyMs: 300_000 },
      target: {
        type: "agent",
        agentId: "16519b37-1614-47f7-92db-01d0208b4529",
      },
      status: "active",
      createdAt: "2026-07-28T16:26:48.515Z",
      updatedAt: "2026-08-01T21:41:50.556Z",
      nextRunAt: "2026-08-01T21:51:48.515Z",
      lastRunAt: "2026-08-01T21:41:50.556Z",
      pausedAt: null,
      expiresAt: null,
      maxRuns: null,
    });
    expect(JSON.stringify(serialized)).not.toContain("secret wake prompt");
    expect(JSON.stringify(serialized)).not.toContain("large output");
  });
});
