import type { Command } from "commander";
import type { ListResult } from "../../output/index.js";
import {
  createScheduleIdentityInspectRows,
  createScheduleIdentityInspectSchema,
  createScheduleInspectRows,
  createScheduleInspectSchema,
  type ScheduleInspectRow,
} from "./schema.js";
import {
  connectScheduleClient,
  toScheduleCommandError,
  type ScheduleCommandOptions,
} from "./shared.js";

interface ScheduleInspectCommandOptions extends ScheduleCommandOptions {
  identityOnly?: boolean;
}

export async function runInspectCommand(
  id: string,
  options: ScheduleInspectCommandOptions,
  _command: Command,
): Promise<ListResult<ScheduleInspectRow>> {
  const { client } = await connectScheduleClient(options.host);
  try {
    if (options.identityOnly) {
      if (client.getLastServerInfoMessage()?.features?.scheduleIdentity !== true) {
        throw {
          code: "DAEMON_UPDATE_REQUIRED",
          message: "This daemon does not support bounded schedule identity inspection.",
          details: "Update the host to use --identity-only.",
        };
      }
      const payload = await client.scheduleIdentity({ id });
      if (payload.error || !payload.schedule) {
        throw new Error(payload.error ?? `Schedule not found: ${id}`);
      }
      return {
        type: "list",
        data: createScheduleIdentityInspectRows(payload.schedule),
        schema: createScheduleIdentityInspectSchema(payload.schedule),
      };
    }
    const payload = await client.scheduleInspect({ id });
    if (payload.error || !payload.schedule) {
      throw new Error(payload.error ?? `Schedule not found: ${id}`);
    }
    const rows = createScheduleInspectRows(payload.schedule);
    return {
      type: "list",
      data: rows,
      schema: createScheduleInspectSchema(payload.schedule),
    };
  } catch (error) {
    throw toScheduleCommandError("SCHEDULE_INSPECT_FAILED", "inspect schedule", error);
  } finally {
    await client.close().catch(() => {});
  }
}
