export interface DesktopStartupDependencies {
  hasPendingOpenProjectPath: boolean;
  runCliPassthroughIfRequested: () => Promise<boolean>;
  inheritLoginShellEnv: () => Promise<void>;
  bootstrapGui: () => Promise<void>;
  autoUpdateInstalledSkills?: () => void;
}

export async function runDesktopStartup(deps: DesktopStartupDependencies): Promise<void> {
  if (!deps.hasPendingOpenProjectPath && (await deps.runCliPassthroughIfRequested())) {
    return;
  }

  await deps.inheritLoginShellEnv();
  await deps.bootstrapGui();
  deps.autoUpdateInstalledSkills?.();
}
