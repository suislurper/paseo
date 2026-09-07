import type { DirectoryWorkspaceLaunchReferenceInspection } from "./directory-workspace-launch-cleanup.js";

export interface DirectoryWorkspaceLaunchReference {
  workspaceId?: string | null;
  archivedAt?: string | null;
}

export interface DirectoryWorkspaceLaunchReferenceTerminalManager {
  listDirectories(): string[];
  getTerminals(
    cwd: string,
    options?: { workspaceId?: string },
  ): Promise<readonly DirectoryWorkspaceLaunchReference[]>;
}

export interface InspectDirectoryWorkspaceLaunchReferencesInput {
  listManagedAgents: () => readonly DirectoryWorkspaceLaunchReference[];
  listPersistedAgents: () => Promise<readonly DirectoryWorkspaceLaunchReference[]>;
  terminalManager: DirectoryWorkspaceLaunchReferenceTerminalManager | null;
}

function hasExactWorkspaceId(
  records: readonly DirectoryWorkspaceLaunchReference[],
  workspaceId: string,
): boolean {
  return records.some((record) => record.workspaceId === workspaceId);
}

function hasInspectableTerminalManager(
  terminalManager: DirectoryWorkspaceLaunchReferenceTerminalManager,
): boolean {
  return (
    typeof terminalManager.listDirectories === "function" &&
    typeof terminalManager.getTerminals === "function"
  );
}

export async function inspectDirectoryWorkspaceLaunchReferences(
  workspaceId: string,
  input: InspectDirectoryWorkspaceLaunchReferencesInput,
): Promise<DirectoryWorkspaceLaunchReferenceInspection> {
  if (typeof input.listManagedAgents !== "function") {
    return { available: false };
  }
  if (typeof input.listPersistedAgents !== "function") {
    return { available: false };
  }

  const managedAgents = input.listManagedAgents();
  const persistedAgents = await input.listPersistedAgents();
  let hasTerminal = false;
  if (input.terminalManager !== null) {
    if (!hasInspectableTerminalManager(input.terminalManager)) {
      return { available: false };
    }
    const directories = input.terminalManager.listDirectories();
    for (const cwd of directories) {
      const terminals = await input.terminalManager.getTerminals(cwd, { workspaceId });
      if (hasExactWorkspaceId(terminals, workspaceId)) {
        hasTerminal = true;
        break;
      }
    }
  }

  return {
    available: true,
    hasManagedAgent: hasExactWorkspaceId(managedAgents, workspaceId),
    hasPersistedAgent: hasExactWorkspaceId(persistedAgents, workspaceId),
    hasTerminal,
  };
}
