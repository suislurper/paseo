import type { WorkspaceCreateRequest } from "@getpaseo/protocol/messages";

export interface FrozenWorkspaceCreationInput {
  source: WorkspaceCreateRequest["source"];
  title?: string;
  firstAgentContext?: WorkspaceCreateRequest["firstAgentContext"];
}

export interface WorkspaceCreationAttempt extends FrozenWorkspaceCreationInput {
  requestId: string;
  /** Capability observed before the original dispatch, not after a host upgrade. */
  retrySupported?: boolean;
}

function randomAttemptId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
}

/** Freeze one GUI creation attempt: stable requestId plus the complete input. */
export function createWorkspaceCreationAttempt(
  input: FrozenWorkspaceCreationInput,
): WorkspaceCreationAttempt {
  const frozen = JSON.parse(JSON.stringify(input)) as FrozenWorkspaceCreationInput;
  return { ...frozen, requestId: randomAttemptId() };
}

export type WorkspaceCreationAttemptErrorKind =
  | "timeout"
  | "connection"
  | "failed"
  | "mismatch"
  | "archived"
  | "ambiguous-target"
  | "host-update-required";

export class WorkspaceCreationAttemptError extends Error {
  readonly kind: WorkspaceCreationAttemptErrorKind;
  readonly retryable: boolean;
  constructor(kind: WorkspaceCreationAttemptErrorKind, message: string, retryable: boolean) {
    super(message);
    this.name = "WorkspaceCreationAttemptError";
    this.kind = kind;
    this.retryable = retryable;
  }
}

function isTimeoutError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /timeout|timed out/i.test(message);
}

function isConnectionError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /transport not connected|connection|network|socket|closed|econn/i.test(message);
}

export interface WorkspaceCreationReply<TWorkspace> {
  workspace: TWorkspace | null;
  error: string | null;
  errorCode?: string | null;
}

export interface WorkspaceCreationClient<TWorkspace extends { id?: string }> {
  supportsWorkspaceCreationRetry(): boolean;
  requireWorkspaceCreationRetrySupport(): void;
  createWorkspace(
    input: FrozenWorkspaceCreationInput,
    requestId?: string,
  ): Promise<WorkspaceCreationReply<TWorkspace>>;
}

export interface WorkspaceCreationAttemptResult<TWorkspace> {
  workspaceId: string;
  workspace: TWorkspace;
}

/**
 * Run one frozen creation attempt. On timeout/connection uncertainty the
 * caller retains the attempt and calls `retryWorkspaceCreationAttempt` with
 * the same ID — never a fresh request. Success or a definitive pre-creation
 * failure clears attempt identity (returns without an attempt to retain).
 */
export async function runWorkspaceCreationAttempt<TWorkspace extends { id?: string }>(input: {
  client: WorkspaceCreationClient<TWorkspace>;
  attempt: WorkspaceCreationAttempt;
  /** COMPAT(workspaceCreationRetry): first creation on old hosts only; remove after 2027-03-11. */
  legacyCreate?: (requestId: string) => Promise<WorkspaceCreationReply<TWorkspace>>;
}): Promise<WorkspaceCreationAttemptResult<TWorkspace>> {
  input.attempt.retrySupported ??= input.client.supportsWorkspaceCreationRetry();
  const dispatch = () =>
    input.legacyCreate
      ? input.legacyCreate(input.attempt.requestId)
      : input.client.createWorkspace(
          {
            source: input.attempt.source,
            ...(input.attempt.title !== undefined ? { title: input.attempt.title } : {}),
            ...(input.attempt.firstAgentContext !== undefined
              ? { firstAgentContext: input.attempt.firstAgentContext }
              : {}),
          },
          input.attempt.requestId,
        );
  const payload = await Promise.resolve()
    .then(dispatch)
    .catch((error: unknown) => {
      if (isTimeoutError(error)) {
        throw new WorkspaceCreationAttemptError(
          "timeout",
          "Workspace creation has not been confirmed. Check again using the same attempt.",
          true,
        );
      }
      if (isConnectionError(error)) {
        throw new WorkspaceCreationAttemptError(
          "connection",
          "Connection lost while creating the workspace. Check again with the same attempt once reconnected.",
          true,
        );
      }
      throw new WorkspaceCreationAttemptError(
        "connection",
        "Workspace creation could not be confirmed. Check the same attempt before creating another workspace.",
        true,
      );
    });
  if (payload.error || !payload.workspace) {
    const code = payload.errorCode ?? null;
    if (code === "creation_request_mismatch") {
      throw new WorkspaceCreationAttemptError(
        "mismatch",
        payload.error ?? "This creation attempt was already used with different input.",
        false,
      );
    }
    if (code === "creation_attempt_archived") {
      throw new WorkspaceCreationAttemptError(
        "archived",
        payload.error ?? "This creation attempt was already archived.",
        false,
      );
    }
    if (code === "creation_target_ambiguous") {
      throw new WorkspaceCreationAttemptError(
        "ambiguous-target",
        payload.error ?? "A previous incomplete creation left files behind.",
        false,
      );
    }
    throw new WorkspaceCreationAttemptError(
      "failed",
      payload.error ?? "Failed to create workspace",
      !["directory_not_found", "source_required", "unknown_project", "archived_project"].includes(
        code ?? "",
      ),
    );
  }
  const workspaceId = typeof payload.workspace.id === "string" ? payload.workspace.id : null;
  if (!workspaceId) {
    throw new WorkspaceCreationAttemptError(
      "failed",
      "Workspace creation completed without a workspace id; inspect the existing attempt.",
      true,
    );
  }
  return { workspaceId, workspace: payload.workspace };
}

/** Reconcile a pending attempt after timeout/connection uncertainty. Same ID. */
export async function retryWorkspaceCreationAttempt<TWorkspace extends { id?: string }>(input: {
  client: WorkspaceCreationClient<TWorkspace>;
  attempt: WorkspaceCreationAttempt;
}): Promise<WorkspaceCreationAttemptResult<TWorkspace>> {
  if (input.attempt.retrySupported === false) {
    throw new WorkspaceCreationAttemptError(
      "host-update-required",
      "The original host did not support safe retries. Inspect its workspaces before starting another creation attempt.",
      true,
    );
  }
  if (!input.client.supportsWorkspaceCreationRetry()) {
    throw new WorkspaceCreationAttemptError(
      "host-update-required",
      "Update the host to retry workspace creation safely.",
      true,
    );
  }
  input.client.requireWorkspaceCreationRetrySupport();
  return runWorkspaceCreationAttempt({ client: input.client, attempt: input.attempt });
}
