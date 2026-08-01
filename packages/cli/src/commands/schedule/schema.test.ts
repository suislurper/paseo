import { describe, expect, test } from "vitest";
import {
  createScheduleIdentityInspectRows,
  createScheduleIdentityInspectSchema,
} from "./schema.js";
import type { ScheduleIdentityRecord } from "./types.js";

function scheduleIdentity(): ScheduleIdentityRecord {
  return {
    id: "fb12e97c",
    cadence: { type: "every", everyMs: 300_000 },
    target: {
      type: "agent",
      agentId: "16519b37-1614-47f7-92db-01d0208b4529",
    },
    status: "active",
    expiresAt: null,
  };
}

describe("bounded schedule identity output", () => {
  test("renders only the server-projected identity", () => {
    const schedule = scheduleIdentity();
    const rows = createScheduleIdentityInspectRows(schedule);
    const schema = createScheduleIdentityInspectSchema(schedule);
    const serialized = schema.serialize?.(rows[0]);

    expect(rows).toEqual([
      { key: "Id", value: "fb12e97c" },
      { key: "Cadence", value: "every:300000ms" },
      {
        key: "Target",
        value: "agent:16519b37-1614-47f7-92db-01d0208b4529",
      },
      { key: "Status", value: "active" },
      { key: "ExpiresAt", value: "null" },
    ]);
    expect(serialized).toEqual({
      id: "fb12e97c",
      cadence: { type: "every", everyMs: 300_000 },
      target: {
        type: "agent",
        agentId: "16519b37-1614-47f7-92db-01d0208b4529",
      },
      status: "active",
      expiresAt: null,
    });
  });
});
