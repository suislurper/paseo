import type { OutputSchema } from "../../output/index.js";
import { formatCadence, formatTarget, type ScheduleRow } from "./shared.js";
import type { ScheduleRecord, ScheduleRunRecord } from "./types.js";

export interface ScheduleIdentityRecord {
  id: string;
  name: string | null;
  cadence: ScheduleRecord["cadence"];
  target: ScheduleRecord["target"];
  status: ScheduleRecord["status"];
  createdAt: string;
  updatedAt: string;
  nextRunAt: string | null;
  lastRunAt: string | null;
  pausedAt: string | null;
  expiresAt: string | null;
  maxRuns: number | null;
}

export const scheduleSchema: OutputSchema<ScheduleRow> = {
  idField: "id",
  columns: [
    { header: "ID", field: "id", width: 10 },
    { header: "NAME", field: "name", width: 20 },
    { header: "CADENCE", field: "cadence", width: 20 },
    { header: "TARGET", field: "target", width: 20 },
    { header: "STATUS", field: "status", width: 12 },
    { header: "NEXT RUN", field: "nextRunAt", width: 24 },
  ],
};

export interface ScheduleInspectRow {
  key: string;
  value: string;
}

export function createScheduleInspectSchema(
  record: ScheduleRecord,
): OutputSchema<ScheduleInspectRow> {
  return {
    idField: "key",
    columns: [
      { header: "KEY", field: "key", width: 18 },
      { header: "VALUE", field: "value", width: 80 },
    ],
    serialize: () => record,
  };
}

export function toScheduleIdentityRecord(record: ScheduleRecord): ScheduleIdentityRecord {
  return {
    id: record.id,
    name: record.name,
    cadence: record.cadence,
    target: record.target,
    status: record.status,
    createdAt: record.createdAt,
    updatedAt: record.updatedAt,
    nextRunAt: record.nextRunAt,
    lastRunAt: record.lastRunAt,
    pausedAt: record.pausedAt,
    expiresAt: record.expiresAt,
    maxRuns: record.maxRuns,
  };
}

export function createScheduleIdentityInspectSchema(
  record: ScheduleRecord,
): OutputSchema<ScheduleInspectRow> {
  const identity = toScheduleIdentityRecord(record);
  return {
    idField: "key",
    columns: [
      { header: "KEY", field: "key", width: 18 },
      { header: "VALUE", field: "value", width: 80 },
    ],
    serialize: () => identity,
  };
}

export interface ScheduleLogRow {
  id: string;
  status: string;
  startedAt: string;
  agentId: string | null;
  output: string | null;
  error: string | null;
}

export const scheduleLogSchema: OutputSchema<ScheduleLogRow> = {
  idField: "id",
  columns: [
    { header: "RUN ID", field: "id", width: 14 },
    { header: "STATUS", field: "status", width: 12 },
    { header: "STARTED", field: "startedAt", width: 24 },
    { header: "AGENT", field: "agentId", width: 12 },
    { header: "OUTPUT", field: "output", width: 40 },
    { header: "ERROR", field: "error", width: 40 },
  ],
};

export function toScheduleLogRow(run: ScheduleRunRecord): ScheduleLogRow {
  return {
    id: run.id,
    status: run.status,
    startedAt: run.startedAt,
    agentId: run.agentId ? run.agentId.slice(0, 7) : null,
    output: run.output,
    error: run.error,
  };
}

export function createScheduleInspectRows(schedule: ScheduleRecord): ScheduleInspectRow[] {
  return [
    { key: "Id", value: schedule.id },
    { key: "Name", value: schedule.name ?? "null" },
    { key: "Prompt", value: schedule.prompt },
    {
      key: "Cadence",
      value:
        schedule.cadence.type === "cron"
          ? formatCadence(schedule.cadence)
          : `every:${schedule.cadence.everyMs}ms`,
    },
    { key: "Target", value: formatTarget(schedule.target) },
    { key: "Status", value: schedule.status },
    { key: "CreatedAt", value: schedule.createdAt },
    { key: "UpdatedAt", value: schedule.updatedAt },
    { key: "NextRunAt", value: schedule.nextRunAt ?? "null" },
    { key: "LastRunAt", value: schedule.lastRunAt ?? "null" },
    { key: "PausedAt", value: schedule.pausedAt ?? "null" },
    { key: "ExpiresAt", value: schedule.expiresAt ?? "null" },
    { key: "MaxRuns", value: schedule.maxRuns == null ? "null" : `${schedule.maxRuns}` },
    { key: "RunCount", value: `${schedule.runs.length}` },
  ];
}

export function createScheduleIdentityInspectRows(schedule: ScheduleRecord): ScheduleInspectRow[] {
  return createScheduleInspectRows(schedule).filter(
    (row) => row.key !== "Prompt" && row.key !== "RunCount",
  );
}
