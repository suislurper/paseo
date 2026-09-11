import { Command } from "commander";
import type { CommandOptions, OutputSchema, SingleResult } from "../../output/index.js";
import { withOutput } from "../../output/index.js";
import { connectToDaemon } from "../../utils/client.js";
import { addJsonAndDaemonHostOptions } from "../../utils/command-options.js";

export interface WorkspaceArchiveResult {
  workspaceId: string;
  archivedAt: string | null;
  cleanup: string;
  reason: string | null;
}

const archiveSchema: OutputSchema<WorkspaceArchiveResult> = {
  idField: "workspaceId",
  columns: [
    { header: "WORKSPACE", field: "workspaceId" },
    { header: "ARCHIVED AT", field: "archivedAt" },
    { header: "FILES", field: "cleanup" },
    { header: "REASON", field: "reason" },
  ],
};

export async function runWorkspaceArchiveCommand(
  workspaceId: string,
  options: CommandOptions & { host?: string; recordOnly?: boolean },
  deps: { connectToDaemon: typeof connectToDaemon } = { connectToDaemon },
): Promise<SingleResult<WorkspaceArchiveResult>> {
  const client = await deps.connectToDaemon({ host: options.host });
  try {
    const response = await client.archiveWorkspace(workspaceId, undefined, {
      mode: options.recordOnly ? "archive_only" : "archive_and_cleanup",
    });
    if (response.error) throw new Error(response.error);
    return {
      type: "single",
      data: {
        workspaceId: response.workspaceId,
        archivedAt: response.archivedAt,
        cleanup: response.cleanup?.status ?? "unknown",
        reason: response.cleanup?.reason ?? null,
      },
      schema: archiveSchema,
    };
  } finally {
    await client.close();
  }
}

export function createWorkspaceCommand(): Command {
  const workspace = new Command("workspace").description("Manage workspace records");
  addJsonAndDaemonHostOptions(
    workspace
      .command("archive")
      .argument("<workspace-id>", "Exact workspace ID")
      .option(
        "--record-only",
        "Archive records and stop owned agents/terminals; retain checkout files",
      )
      .description("Archive a workspace and report whether eligible checkout files were removed"),
  ).action(
    withOutput(
      (
        workspaceId: string,
        options: CommandOptions & { host?: string; recordOnly?: boolean },
        _command: Command,
      ) => runWorkspaceArchiveCommand(workspaceId, options),
    ),
  );
  return workspace;
}
