import type { Logger } from "pino";

import { PARENT_AGENT_ID_LABEL } from "@getpaseo/protocol/agent-labels";
import type { TerminalManager } from "../../../terminal/terminal-manager.js";
import type { CreatePaseoWorktreeInput } from "../../paseo-worktree-service.js";
import { expandUserPath, resolvePathFromBase } from "../../path-utils.js";
import { toWorktreeRequestError } from "../../worktree-errors.js";
import type {
  AgentWorktreeSetupContinuation,
  CreatePaseoWorktreeSetupContinuationInput,
  CreatePaseoWorktreeWorkflowFn,
  CreatePaseoWorktreeWorkflowResult,
} from "../../worktree-session.js";
import type { AgentAttachment, FirstAgentContext, GitSetupOptions } from "../../messages.js";
import type { AgentManager, CreateAgentOptions, ManagedAgent } from "../agent-manager.js";
import type { AgentPromptInput, AgentRunOptions, AgentSessionConfig } from "../agent-sdk-types.js";
import type { AgentStorage } from "../agent-storage.js";
import type { AgentOwner } from "../agent-owner.js";
import type { ProviderSnapshotManager } from "../provider-snapshot-manager.js";
import { setupFinishNotification, startCreatedAgentInitialPrompt } from "../agent-prompt.js";
import { resolveCreateAgentTitles, resolveFirstAgentPromptTitle } from "../create-agent-title.js";
import { buildAgentPrompt } from "../prompt-attachments.js";
import { normalizeClientMessageId, resolveClientMessageId } from "../../client-message-id.js";
import { resolveRequiredProviderModel, type ResolvedProviderModel } from "../mcp-shared.js";
import {
  appendTimelineItemIfAgentKnown,
  emitLiveTimelineItemIfAgentKnown,
} from "../timeline-append.js";
import type {
  AllocateDirectoryWorkspaceForLaunchInput,
  DirectoryWorkspaceLaunch,
} from "../../session/workspace-provisioning/workspace-provisioning-service.js";

export interface CreateAgentSessionWorktreeResult {
  sessionConfig: AgentSessionConfig;
  setupContinuation?: AgentWorktreeSetupContinuation;
  // Set when this build created a fresh worktree workspace. The agent must be
  // stamped with it so workspaceId-scoped archive can find the agent later.
  createdWorkspaceId?: string;
}

export interface McpCreateAgentLaunchProvisioning {
  allocateDirectoryWorkspaceForLaunch(
    input: AllocateDirectoryWorkspaceForLaunchInput,
  ): Promise<DirectoryWorkspaceLaunch>;
  requireExistingWorkspaceForLaunch(workspaceId: string): Promise<{ cwd: string }>;
}

export interface DirectoryWorkspaceCommittedInput {
  workspaceId: string;
  cwd: string;
  firstAgentContext: FirstAgentContext;
}

export interface CreateAgentCommandDependencies {
  agentManager: AgentManager;
  agentStorage: AgentStorage;
  logger: Logger;
  paseoHome?: string;
  worktreesRoot?: string;
  terminalManager?: TerminalManager | null;
  providerSnapshotManager: Pick<ProviderSnapshotManager, "resolveCreateConfig">;
  createPaseoWorktree?: CreatePaseoWorktreeWorkflowFn;
  mcpLaunchProvisioning?: McpCreateAgentLaunchProvisioning;
  onDirectoryWorkspaceCommitted?: (input: DirectoryWorkspaceCommittedInput) => void | Promise<void>;
  onDirectoryWorkspaceCleanup?: (workspaceId: string) => void | Promise<void>;
}

export type EnsureWorkspaceForCreate = (
  cwd: string,
  firstAgentContext?: FirstAgentContext,
) => Promise<string>;

export type McpCreateAgentWorkspaceLaunch =
  | { kind: "attach"; workspaceId: string }
  | { kind: "directory" };

export interface CreateAgentFromSessionInput {
  kind: "session";
  config: AgentSessionConfig;
  workspaceId: string;
  worktreeName?: string;
  initialPrompt?: string;
  clientMessageId?: string;
  outputSchema?: Record<string, unknown>;
  images?: Array<{ data: string; mimeType: string }>;
  attachments?: AgentAttachment[];
  git?: GitSetupOptions;
  labels: Record<string, string>;
  env?: Record<string, string>;
  provisionalTitle: string | null;
  firstAgentContext: FirstAgentContext;
  buildSessionConfig: (
    config: AgentSessionConfig,
    gitOptions?: GitSetupOptions,
    legacyWorktreeName?: string,
    firstAgentContext?: FirstAgentContext,
  ) => Promise<CreateAgentSessionWorktreeResult>;
}

export interface CreateAgentFromMcpInput {
  kind: "mcp";
  provider: string;
  title: string;
  initialPrompt?: string;
  config?: Partial<AgentSessionConfig>;
  cwd?: string;
  workspaceId?: string;
  workspaceLaunch?: McpCreateAgentWorkspaceLaunch;
  thinking?: string;
  features?: Record<string, unknown>;
  labels?: Record<string, string>;
  mode?: string;
  unattended?: boolean;
  promptFailure?: CreateAgentPromptFailureMode;
  background: boolean;
  notifyOnFinish: boolean;
  internal?: boolean;
  detached?: boolean;
  owner?: AgentOwner;
  env?: Record<string, string>;
  onCreated?: (created: {
    agentId: string;
    createdWorktree: CreatePaseoWorktreeWorkflowResult | null;
  }) => void;
  onWorktreeCreated?: (createdWorktree: CreatePaseoWorktreeWorkflowResult) => void;
  callerAgentId?: string;
  callerContext?: {
    lockedCwd?: string;
    allowCustomCwd?: boolean;
    childAgentDefaultLabels?: Record<string, string>;
  } | null;
  worktree?: {
    worktreeName?: string;
    branchName?: string;
    baseBranch?: string;
    refName?: string;
    action?: "branch-off" | "checkout";
    githubPrNumber?: number;
  };
}

export type CreateAgentCommandInput = CreateAgentFromSessionInput | CreateAgentFromMcpInput;
export type CreateAgentPromptFailureMode = "throw" | "log" | "return-error";

export interface CreateAgentCommandResult {
  snapshot: ManagedAgent;
  liveSnapshot: ManagedAgent;
  background: boolean;
  initialPromptStarted: boolean;
  initialPromptError: unknown | null;
  createdWorktree?: CreatePaseoWorktreeWorkflowResult;
}

export type BoundCreateAgentCommand = (
  input: CreateAgentCommandInput,
) => Promise<CreateAgentCommandResult>;

function requireResolvedWorkspaceId(workspaceId: string | undefined): string {
  if (!workspaceId) {
    throw new Error("createAgentCommand requires a resolved workspaceId");
  }
  return workspaceId;
}

export function formatProviderModel(provider: string, model: string | null | undefined): string {
  if (!model || provider.includes("/")) {
    return provider;
  }
  return `${provider}/${model}`;
}

function resolveProviderModel(providerValue: string): ResolvedProviderModel {
  const providerInput = providerValue.trim();
  if (providerInput.includes("/")) {
    return resolveRequiredProviderModel(providerInput);
  }
  if (!providerInput) {
    throw new Error("provider is required");
  }
  return { provider: providerInput, model: undefined };
}

interface ResolvedCreateAgent {
  config: AgentSessionConfig;
  createOptions: CreateAgentOptions;
  prompt?: AgentPromptInput;
  runOptions?: AgentRunOptions;
  setupContinuation?: AgentWorktreeSetupContinuation;
  background: boolean;
  promptFailure: CreateAgentPromptFailureMode;
  promptLogger?: Logger;
  createdWorktree?: CreatePaseoWorktreeWorkflowResult;
}

export async function createAgentCommand(
  dependencies: CreateAgentCommandDependencies,
  input: CreateAgentCommandInput,
): Promise<CreateAgentCommandResult> {
  if (input.kind === "session") {
    const resolved = await resolveSessionCreateAgent(dependencies, input);
    return finishCreatedAgent(dependencies, input, resolved);
  }

  const directoryLaunchState: { current: DirectoryWorkspaceLaunch | null } = { current: null };
  try {
    const resolved = await resolveMcpCreateAgent(dependencies, input, {
      onDirectoryLaunch(launch) {
        directoryLaunchState.current = launch;
      },
    });
    const result = await finishCreatedAgent(dependencies, input, resolved, {
      beforePostCreate: async () => {
        const directoryLaunch = directoryLaunchState.current;
        if (!directoryLaunch) return;
        directoryLaunch.retain();
        directoryLaunch.commit();
        directoryLaunchState.current = null;
        await invokeGuardedWorkspaceCallback({
          callback: dependencies.onDirectoryWorkspaceCommitted
            ? () =>
                dependencies.onDirectoryWorkspaceCommitted?.({
                  workspaceId: directoryLaunch.workspaceId,
                  cwd: resolved.config.cwd,
                  firstAgentContext: mcpFirstAgentContext(input),
                })
            : undefined,
          workspaceId: directoryLaunch.workspaceId,
          logger: dependencies.logger,
          message: "Failed to run directory workspace success side effects",
        });
      },
    });
    return result;
  } catch (error) {
    const directoryLaunch = directoryLaunchState.current;
    if (directoryLaunch) {
      const failedWorkspaceId = directoryLaunch.workspaceId;
      await directoryLaunch.cleanupUnusedOnFailure();
      await invokeGuardedWorkspaceCallback({
        callback: dependencies.onDirectoryWorkspaceCleanup
          ? () => dependencies.onDirectoryWorkspaceCleanup?.(failedWorkspaceId)
          : undefined,
        workspaceId: failedWorkspaceId,
        logger: dependencies.logger,
        message: "Failed to emit directory workspace cleanup update",
      });
    }
    throw error;
  }
}

async function finishCreatedAgent(
  dependencies: CreateAgentCommandDependencies,
  input: CreateAgentCommandInput,
  resolved: ResolvedCreateAgent,
  hooks?: { beforePostCreate?: () => void | Promise<void> },
): Promise<CreateAgentCommandResult> {
  const snapshot = await dependencies.agentManager.createAgent(
    resolved.config,
    undefined,
    resolved.createOptions,
  );
  await hooks?.beforePostCreate?.();

  resolved.setupContinuation?.startAfterAgentCreate({
    agentId: snapshot.id,
  });

  let liveSnapshot = snapshot;
  let initialPromptStarted = false;
  let initialPromptError: unknown | null = null;
  if (input.kind === "mcp") {
    input.onCreated?.({ agentId: snapshot.id, createdWorktree: resolved.createdWorktree ?? null });
  }
  if (resolved.prompt !== undefined) {
    const sendResult = await sendInitialPrompt(dependencies, resolved, snapshot);
    initialPromptStarted = sendResult.started;
    liveSnapshot = sendResult.liveSnapshot;
    initialPromptError = sendResult.error ?? null;
  }

  if (input.kind === "mcp" && input.notifyOnFinish && input.callerAgentId && initialPromptStarted) {
    setupFinishNotification({
      agentManager: dependencies.agentManager,
      agentStorage: dependencies.agentStorage,
      childAgentId: snapshot.id,
      callerAgentId: input.callerAgentId,
      logger: dependencies.logger,
    });
  }

  return {
    snapshot,
    liveSnapshot,
    background: resolved.background,
    initialPromptStarted,
    initialPromptError,
    ...(resolved.createdWorktree ? { createdWorktree: resolved.createdWorktree } : {}),
  };
}

async function resolveSessionCreateAgent(
  dependencies: CreateAgentCommandDependencies,
  input: CreateAgentFromSessionInput,
): Promise<ResolvedCreateAgent> {
  const trimmedPrompt = input.initialPrompt?.trim();
  const {
    sessionConfig: builtSessionConfig,
    setupContinuation,
    createdWorkspaceId,
  } = await input.buildSessionConfig(
    input.config,
    input.git,
    input.worktreeName,
    input.firstAgentContext,
  );
  // Validate the requested mode against the provider's modes for the resolved
  // cwd. The app remembers mode preferences globally, so a saved mode can be
  // stale for a workspace whose provider config no longer defines it — reject
  // it here instead of letting the provider fail mid-turn.
  //
  // This runs after buildSessionConfig, which may already have created a
  // worktree and/or workspace record — cwd (required to resolve modes) is
  // only known once that step completes. If validation throws, the enclosing
  // Session owns cleanup: handleCreateAgentRequest tears down a failed
  // worktree via cleanupCreatedWorktreeAfterFailedAgentCreate, and archives
  // an unused request-owned directory workspace via
  // directoryLaunch.cleanupUnusedOnFailure.
  const resolvedCreateConfig = await dependencies.providerSnapshotManager.resolveCreateConfig({
    cwd: builtSessionConfig.cwd,
    provider: builtSessionConfig.provider,
    requestedMode: builtSessionConfig.modeId,
    featureValues: builtSessionConfig.featureValues,
    parent: null,
    unattended: false,
  });
  const sessionConfig: AgentSessionConfig = {
    ...builtSessionConfig,
    modeId: resolvedCreateConfig.modeId,
    featureValues: resolvedCreateConfig.featureValues,
  };
  const prompt = buildAgentPrompt(trimmedPrompt ?? "", input.images, input.attachments);
  const hasPromptContent = Array.isArray(prompt) ? prompt.length > 0 : prompt.length > 0;
  const clientMessageId = normalizeClientMessageId(input.clientMessageId);
  const runOptions: AgentRunOptions | undefined =
    input.outputSchema || clientMessageId
      ? {
          ...(input.outputSchema ? { outputSchema: input.outputSchema } : {}),
          ...(clientMessageId ? { messageId: clientMessageId } : {}),
        }
      : undefined;
  const workspaceId = setupContinuation ? createdWorkspaceId : input.workspaceId;

  return {
    config: sessionConfig,
    createOptions: {
      labels: input.labels,
      initialPrompt: trimmedPrompt,
      env: input.env,
      initialTitle: input.provisionalTitle,
      // A legacy git/worktreeName worktree creates a fresh workspace, so the
      // agent belongs to that workspace, not the source one. createdWorkspaceId
      // is the freshly created worktree's workspace.
      workspaceId: requireResolvedWorkspaceId(workspaceId),
    },
    prompt: hasPromptContent ? prompt : undefined,
    runOptions,
    setupContinuation,
    background: true,
    promptFailure: "throw",
    promptLogger: dependencies.logger.child({
      clientMessageId: resolveClientMessageId(input.clientMessageId),
    }),
  };
}

interface ResolveMcpCreateAgentCallbacks {
  onDirectoryLaunch(launch: DirectoryWorkspaceLaunch): void;
}

type McpWorkspacePlan =
  | {
      kind: "worktree";
      resolvedCwd: string;
      workspaceId: string;
      setupContinuation?: AgentWorktreeSetupContinuation;
      createdWorktree?: CreatePaseoWorktreeWorkflowResult;
    }
  | {
      kind: "directory";
      resolvedCwd: string;
      workspaceId: string;
    }
  | {
      kind: "attach";
      resolvedCwd: string;
      workspaceId: string;
    };

async function resolveMcpCreateAgent(
  dependencies: CreateAgentCommandDependencies,
  input: CreateAgentFromMcpInput,
  callbacks: ResolveMcpCreateAgentCallbacks,
): Promise<ResolvedCreateAgent> {
  const launchProvisioning = requireMcpLaunchProvisioning(dependencies);
  const resolvedProviderModel = resolveProviderModel(input.provider);
  const provider = resolvedProviderModel.provider;
  const parentAgent = input.callerAgentId
    ? requireParentAgent(dependencies.agentManager, input.callerAgentId)
    : null;
  const plan = await resolveMcpWorkspacePlan({
    dependencies,
    input,
    parentAgent,
    launchProvisioning,
    onDirectoryLaunch: callbacks.onDirectoryLaunch,
  });
  const resolvedCreateConfig = await resolveMcpProviderCreateConfig({
    dependencies,
    input,
    provider,
    resolvedCwd: plan.resolvedCwd,
    parentAgent,
  });

  const labels = mergeLabels({
    callerAgentId: input.callerAgentId,
    detached: input.detached ?? false,
    childAgentDefaultLabels: input.callerContext?.childAgentDefaultLabels,
    labels: input.labels,
  });

  const trimmedPrompt = input.initialPrompt?.trim() ?? "";
  return {
    config: buildMcpSessionConfig({
      input,
      resolvedProviderModel,
      provider,
      resolvedCwd: plan.resolvedCwd,
      trimmedPrompt,
      resolvedMode: resolvedCreateConfig.modeId,
      resolvedFeatures: resolvedCreateConfig.featureValues,
    }),
    createOptions: {
      ...(labels ? { labels } : {}),
      workspaceId: plan.workspaceId,
      owner: input.owner,
      env: input.env,
    },
    prompt: trimmedPrompt ? trimmedPrompt : undefined,
    setupContinuation: plan.kind === "worktree" ? plan.setupContinuation : undefined,
    createdWorktree: plan.kind === "worktree" ? plan.createdWorktree : undefined,
    background: input.background,
    promptFailure: input.promptFailure ?? "log",
  };
}

function requireMcpLaunchProvisioning(
  dependencies: CreateAgentCommandDependencies,
): McpCreateAgentLaunchProvisioning {
  if (!dependencies.mcpLaunchProvisioning) {
    throw new Error("MCP create-agent launch provisioning is not configured");
  }
  return dependencies.mcpLaunchProvisioning;
}

async function resolveMcpWorkspacePlan(params: {
  dependencies: CreateAgentCommandDependencies;
  input: CreateAgentFromMcpInput;
  parentAgent: ManagedAgent | null;
  launchProvisioning: McpCreateAgentLaunchProvisioning;
  onDirectoryLaunch: (launch: DirectoryWorkspaceLaunch) => void;
}): Promise<McpWorkspacePlan> {
  const { dependencies, input, parentAgent, launchProvisioning, onDirectoryLaunch } = params;

  if (shouldCreateMcpWorktree(input.worktree)) {
    const initialCwd = resolveMcpInitialCwd(input, parentAgent);
    const { resolvedCwd, setupContinuation, createdWorkspaceId, createdWorktree } =
      await resolveMcpCwd({
        dependencies,
        cwd: initialCwd,
        worktree: input.worktree,
        initialPrompt: input.initialPrompt ?? "",
      });
    if (createdWorktree) input.onWorktreeCreated?.(createdWorktree);
    return {
      kind: "worktree",
      resolvedCwd,
      workspaceId: requireResolvedWorkspaceId(createdWorkspaceId),
      setupContinuation,
      createdWorktree,
    };
  }

  if (input.workspaceLaunch?.kind === "directory") {
    return allocateOwnedDirectoryWorkspace({
      launchProvisioning,
      cwd: resolveMcpInitialCwd(input, parentAgent),
      title: resolveFirstAgentPromptTitle({ prompt: input.initialPrompt }),
      onDirectoryLaunch,
    });
  }

  const attachWorkspaceId = resolveMcpAttachWorkspaceId(input, parentAgent);
  if (attachWorkspaceId) {
    await launchProvisioning.requireExistingWorkspaceForLaunch(attachWorkspaceId);
    return {
      kind: "attach",
      resolvedCwd: resolveMcpInitialCwd(input, parentAgent),
      workspaceId: attachWorkspaceId,
    };
  }

  return allocateOwnedDirectoryWorkspace({
    launchProvisioning,
    cwd: resolveMcpInitialCwd(input, parentAgent),
    title: resolveFirstAgentPromptTitle({ prompt: input.initialPrompt }),
    onDirectoryLaunch,
  });
}

function resolveMcpAttachWorkspaceId(
  input: CreateAgentFromMcpInput,
  parentAgent: ManagedAgent | null,
): string | null {
  if (input.workspaceLaunch?.kind === "attach") {
    return input.workspaceLaunch.workspaceId;
  }
  if (input.workspaceId) {
    return input.workspaceId;
  }
  if (parentAgent?.workspaceId) {
    return parentAgent.workspaceId;
  }
  return null;
}

async function allocateOwnedDirectoryWorkspace(params: {
  launchProvisioning: McpCreateAgentLaunchProvisioning;
  cwd: string;
  title: string | null;
  onDirectoryLaunch: (launch: DirectoryWorkspaceLaunch) => void;
}): Promise<Extract<McpWorkspacePlan, { kind: "directory" }>> {
  const launch = await params.launchProvisioning.allocateDirectoryWorkspaceForLaunch({
    cwd: params.cwd,
    title: params.title,
  });
  params.onDirectoryLaunch(launch);
  return {
    kind: "directory",
    resolvedCwd: params.cwd,
    workspaceId: launch.workspaceId,
  };
}

function resolveMcpInitialCwd(
  input: CreateAgentFromMcpInput,
  parentAgent: ManagedAgent | null,
): string {
  if (!parentAgent) {
    return expandUserPath(input.cwd ?? process.cwd());
  }
  return resolveChildAgentCwd({
    parentCwd: parentAgent.cwd,
    requestedCwd: input.cwd,
    lockedCwd: input.callerContext?.lockedCwd,
    allowCustomCwd: input.callerContext?.allowCustomCwd ?? true,
  });
}

async function resolveMcpProviderCreateConfig(params: {
  dependencies: CreateAgentCommandDependencies;
  input: CreateAgentFromMcpInput;
  provider: string;
  resolvedCwd: string;
  parentAgent: ManagedAgent | null;
}): Promise<{ modeId?: string; featureValues?: Record<string, unknown> }> {
  const passthroughConfig = params.input.config;
  return params.dependencies.providerSnapshotManager.resolveCreateConfig({
    cwd: params.resolvedCwd,
    provider: params.provider,
    requestedMode: params.input.mode ?? passthroughConfig?.modeId,
    featureValues: params.input.features ?? passthroughConfig?.featureValues,
    parent: params.parentAgent,
    unattended: params.input.unattended ?? false,
  });
}

function buildMcpSessionConfig(params: {
  input: CreateAgentFromMcpInput;
  resolvedProviderModel: ResolvedProviderModel;
  provider: string;
  resolvedCwd: string;
  trimmedPrompt: string;
  resolvedMode?: string;
  resolvedFeatures?: Record<string, unknown>;
}): AgentSessionConfig {
  const passthroughConfig = params.input.config;
  const { provisionalTitle } = resolveCreateAgentTitles({
    configTitle: passthroughConfig?.title ?? params.input.title,
    initialPrompt: params.trimmedPrompt,
  });
  const featureValues = params.resolvedFeatures ?? passthroughConfig?.featureValues;
  const config: AgentSessionConfig = {
    ...passthroughConfig,
    provider: params.provider,
    cwd: params.resolvedCwd,
    modeId: params.resolvedMode ?? passthroughConfig?.modeId,
    model: params.resolvedProviderModel.model ?? passthroughConfig?.model,
    thinkingOptionId: params.input.thinking ?? passthroughConfig?.thinkingOptionId,
    internal: params.input.internal ?? passthroughConfig?.internal,
  };
  if (provisionalTitle) {
    config.title = provisionalTitle;
  }
  if (featureValues) {
    config.featureValues = featureValues;
  }
  return config;
}

function mcpFirstAgentContext(input: CreateAgentFromMcpInput): FirstAgentContext {
  const prompt = input.initialPrompt?.trim();
  return prompt ? { prompt } : {};
}

async function invokeGuardedWorkspaceCallback(params: {
  callback: (() => void | Promise<void>) | undefined;
  workspaceId: string;
  logger: Logger;
  message: string;
}): Promise<void> {
  if (!params.callback) return;
  try {
    await params.callback();
  } catch (error) {
    params.logger.error({ err: error, workspaceId: params.workspaceId }, params.message);
  }
}

async function sendInitialPrompt(
  dependencies: CreateAgentCommandDependencies,
  resolved: ResolvedCreateAgent,
  snapshot: ManagedAgent,
): Promise<{ started: boolean; liveSnapshot: ManagedAgent; error?: unknown }> {
  try {
    const prompt = resolved.prompt;
    if (prompt === undefined) {
      return { started: false, liveSnapshot: snapshot };
    }
    const liveSnapshot = await startCreatedAgentInitialPrompt({
      agentManager: dependencies.agentManager,
      agentId: snapshot.id,
      snapshot,
      prompt,
      runOptions: resolved.runOptions,
      logger: resolved.promptLogger ?? dependencies.logger,
    });
    return { started: true, liveSnapshot };
  } catch (error) {
    if (resolved.promptFailure === "throw") {
      throw error;
    }
    if (resolved.promptFailure === "return-error") {
      return { started: false, liveSnapshot: snapshot, error };
    }
    dependencies.logger.error({ err: error, agentId: snapshot.id }, "Failed to run initial prompt");
    return { started: false, liveSnapshot: snapshot };
  }
}

function requireParentAgent(agentManager: AgentManager, parentAgentId: string): ManagedAgent {
  const parentAgent = agentManager.getAgent(parentAgentId);
  if (!parentAgent) {
    throw new Error(`Parent agent ${parentAgentId} not found`);
  }
  return parentAgent;
}

function resolveChildAgentCwd(params: {
  parentCwd: string;
  requestedCwd?: string;
  lockedCwd?: string;
  allowCustomCwd: boolean;
}): string {
  const lockedCwd = params.lockedCwd?.trim();
  if (lockedCwd) {
    return expandUserPath(lockedCwd);
  }

  const requestedCwd = params.requestedCwd?.trim();
  if (!requestedCwd || !params.allowCustomCwd) {
    return params.parentCwd;
  }

  return resolvePathFromBase(params.parentCwd, requestedCwd);
}

function shouldCreateMcpWorktree(
  worktree: CreateAgentFromMcpInput["worktree"],
): worktree is NonNullable<CreateAgentFromMcpInput["worktree"]> {
  if (!worktree) return false;
  return Boolean(
    worktree.worktreeName || worktree.refName || worktree.action || worktree.githubPrNumber,
  );
}

async function resolveMcpCwd(params: {
  dependencies: CreateAgentCommandDependencies;
  cwd: string;
  initialPrompt: string;
  worktree: CreateAgentFromMcpInput["worktree"];
}): Promise<{
  resolvedCwd: string;
  setupContinuation?: AgentWorktreeSetupContinuation;
  createdWorkspaceId?: string;
  createdWorktree?: CreatePaseoWorktreeWorkflowResult;
}> {
  const { dependencies, worktree } = params;
  if (!shouldCreateMcpWorktree(worktree)) {
    return { resolvedCwd: params.cwd };
  }
  if (
    worktree.worktreeName &&
    !worktree.baseBranch &&
    !worktree.refName &&
    !worktree.action &&
    worktree.githubPrNumber === undefined
  ) {
    throw new Error("baseBranch is required when creating a worktree");
  }
  const baseBranch = worktree.baseBranch;
  const createdWorktree = await createMcpWorktree({
    input: {
      cwd: params.cwd,
      worktreeSlug: worktree.worktreeName,
      branchName: worktree.branchName,
      refName: worktree.refName,
      action: worktree.action,
      githubPrNumber: worktree.githubPrNumber,
      firstAgentContext: { prompt: params.initialPrompt },
      runSetup: false,
      paseoHome: dependencies.paseoHome,
      worktreesRoot: dependencies.worktreesRoot,
    },
    createPaseoWorktree: dependencies.createPaseoWorktree,
    resolveDefaultBranch: baseBranch ? async () => baseBranch : undefined,
    setupContinuation: {
      kind: "agent",
      terminalManager: dependencies.terminalManager ?? null,
      appendTimelineItem: ({ agentId, item }) =>
        appendTimelineItemIfAgentKnown({
          agentManager: dependencies.agentManager,
          agentId,
          item,
        }),
      emitLiveTimelineItem: ({ agentId, item }) =>
        emitLiveTimelineItemIfAgentKnown({
          agentManager: dependencies.agentManager,
          agentId,
          item,
        }),
      logger: dependencies.logger,
    },
  });
  return {
    resolvedCwd: createdWorktree.workspace.cwd,
    setupContinuation: createdWorktree.setupContinuation,
    createdWorkspaceId: createdWorktree.workspace.workspaceId,
    createdWorktree,
  };
}

interface CreateMcpWorktreeOptions {
  input: CreatePaseoWorktreeInput;
  createPaseoWorktree: CreatePaseoWorktreeWorkflowFn | undefined;
  resolveDefaultBranch?: (repoRoot: string) => Promise<string>;
  setupContinuation?: CreatePaseoWorktreeSetupContinuationInput;
}

async function createMcpWorktree(
  options: CreateMcpWorktreeOptions,
): Promise<CreatePaseoWorktreeWorkflowResult> {
  try {
    if (!options.createPaseoWorktree) {
      throw new Error("Paseo worktree service is not configured");
    }
    return await options.createPaseoWorktree(options.input, {
      ...(options.resolveDefaultBranch
        ? { resolveDefaultBranch: options.resolveDefaultBranch }
        : {}),
      ...(options.setupContinuation ? { setupContinuation: options.setupContinuation } : {}),
    });
  } catch (error) {
    throw toWorktreeRequestError(error);
  }
}

function mergeLabels(params: {
  callerAgentId: string | undefined;
  detached: boolean;
  childAgentDefaultLabels: Record<string, string> | undefined;
  labels: Record<string, string> | undefined;
}): Record<string, string> | undefined {
  const mergedLabels = {
    ...(!params.detached && params.callerAgentId
      ? { [PARENT_AGENT_ID_LABEL]: params.callerAgentId }
      : {}),
    ...params.childAgentDefaultLabels,
    ...params.labels,
  };
  if (params.detached) {
    delete mergedLabels[PARENT_AGENT_ID_LABEL];
  }
  return Object.keys(mergedLabels).length > 0 ? mergedLabels : undefined;
}
