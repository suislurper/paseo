import { createHash } from "node:crypto";
import type {
  WorkspaceCreateRequest,
  WorkspaceDescriptorPayload,
} from "@getpaseo/protocol/messages";

/**
 * Identity of one workspace.create attempt. The envelope requestId is the
 * attempt key; the fingerprint freezes the complete create input (including
 * the generated worktree slug) so a retry with the same ID replays safely
 * while the same ID with different input fails explicitly.
 */
export interface WorkspaceCreationIdentityInput {
  requestId: string;
  title?: string | null;
  firstAgentContext?: WorkspaceCreateRequest["firstAgentContext"];
  source: WorkspaceCreateRequest["source"];
}

export interface WorkspaceCreationOutcome {
  workspace: WorkspaceDescriptorPayload | null;
  error: string | null;
  errorCode?: string;
  reconciled: boolean;
  archived: boolean;
}

function stableStringify(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item)).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => {
        if (left < right) return -1;
        return left > right ? 1 : 0;
      })
      .map(([key, item]) => `${JSON.stringify(key)}:${stableStringify(item)}`);
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function normalizeSource(source: WorkspaceCreateRequest["source"]): unknown {
  if (source.kind === "directory") {
    return {
      kind: "directory" as const,
      path: source.path,
      ...(source.projectId !== undefined ? { projectId: source.projectId } : {}),
    };
  }
  return {
    kind: "worktree" as const,
    ...(source.cwd !== undefined ? { cwd: source.cwd } : {}),
    ...(source.projectId !== undefined ? { projectId: source.projectId } : {}),
    ...(source.action !== undefined ? { action: source.action } : {}),
    ...(source.refName !== undefined ? { refName: source.refName } : {}),
    ...(source.baseBranch !== undefined ? { baseBranch: source.baseBranch } : {}),
    ...(source.checkoutSource !== undefined ? { checkoutSource: source.checkoutSource } : {}),
    ...(source.githubPrNumber !== undefined ? { githubPrNumber: source.githubPrNumber } : {}),
    ...(source.worktreeSlug !== undefined ? { worktreeSlug: source.worktreeSlug } : {}),
  };
}

function normalizeFirstAgentContext(context: WorkspaceCreateRequest["firstAgentContext"]): unknown {
  if (!context) return null;
  return {
    ...(context.prompt !== undefined ? { prompt: context.prompt } : {}),
    attachments: Array.isArray(context.attachments) ? context.attachments : [],
  };
}

export function computeWorkspaceCreationFingerprint(input: WorkspaceCreationIdentityInput): string {
  const frozen = {
    requestId: input.requestId,
    title: input.title?.trim() || null,
    firstAgentContext: normalizeFirstAgentContext(input.firstAgentContext),
    source: normalizeSource(input.source),
  };
  return createHash("sha256").update(stableStringify(frozen)).digest("hex");
}

interface WorkspaceCreationFlight {
  fingerprint: string;
  promise: Promise<WorkspaceCreationOutcome>;
}

type WorkspaceCreationRunner = () => Promise<WorkspaceCreationOutcome>;

/**
 * Daemon-scope coalescer for workspace.create attempts. Keyed by the caller
 * WeakMap owner (the shared workspace registry) so simultaneous identical
 * request IDs from any socket share one execution. Different IDs never
 * coalesce, preserving intentional same-directory duplicates.
 */
const flightsByRegistry = new WeakMap<object, Map<string, WorkspaceCreationFlight>>();

function flightsFor(owner: object): Map<string, WorkspaceCreationFlight> {
  const existing = flightsByRegistry.get(owner);
  if (existing) return existing;
  const next = new Map<string, WorkspaceCreationFlight>();
  flightsByRegistry.set(owner, next);
  return next;
}

export async function runWorkspaceCreationIdempotent(params: {
  owner: object;
  requestId: string;
  fingerprint: string;
  runner: WorkspaceCreationRunner;
}): Promise<{ outcome: WorkspaceCreationOutcome; reconciled: boolean }> {
  const flights = flightsFor(params.owner);
  const inFlight = flights.get(params.requestId);
  if (inFlight) {
    if (inFlight.fingerprint !== params.fingerprint) {
      throw new WorkspaceCreationMismatchError(params.requestId);
    }
    const outcome = await inFlight.promise;
    return { outcome, reconciled: true };
  }
  const flight: WorkspaceCreationFlight = {
    fingerprint: params.fingerprint,
    promise: (async () => params.runner())(),
  };
  flights.set(params.requestId, flight);
  try {
    const outcome = await flight.promise;
    return { outcome, reconciled: false };
  } finally {
    if (flights.get(params.requestId) === flight) {
      flights.delete(params.requestId);
    }
  }
}

export class WorkspaceCreationMismatchError extends Error {
  readonly requestId: string;
  constructor(requestId: string) {
    super(
      `Workspace creation request ID "${requestId}" was already used with different input; start a new workspace action for a new attempt.`,
    );
    this.name = "WorkspaceCreationMismatchError";
    this.requestId = requestId;
  }
}
