export type WorkspaceCreationReconciliationCode = "creation_target_ambiguous";

export class WorkspaceCreationReconciliationError extends Error {
  readonly code: WorkspaceCreationReconciliationCode;
  constructor(code: WorkspaceCreationReconciliationCode, message: string) {
    super(message);
    this.name = "WorkspaceCreationReconciliationError";
    this.code = code;
  }
}
