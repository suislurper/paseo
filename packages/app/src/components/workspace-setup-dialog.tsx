import { WorkspaceCreationRetryButton } from "@/components/workspace-creation-retry-button";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Image, Text, View } from "react-native";
import { StyleSheet } from "react-native-unistyles";
import { useTranslation } from "react-i18next";
import { createNameId } from "mnemonic-id";
import { AdaptiveModalSheet, type SheetHeader } from "@/components/adaptive-modal-sheet";
import { FileDropZone } from "@/components/file-drop/file-drop-zone";
import { Composer } from "@/composer";
import { DraftAgentModeControl } from "@/composer/agent-controls/mode-control";
import { useToast } from "@/contexts/toast-context";
import { useAgentInputDraft } from "@/composer/draft/input-draft";
import { useProjectIconQuery } from "@/hooks/use-project-icon-query";
import { useIsCompactFormFactor } from "@/constants/layout";
import { useHostRuntimeClient, useHostRuntimeIsConnected } from "@/runtime/host-runtime";
import { normalizeWorkspaceDescriptor, useSessionStore } from "@/stores/session-store";
import { useWorkspaceSetupStore } from "@/stores/workspace-setup-store";
import { normalizeAgentSnapshot } from "@/utils/agent-snapshots";
import { applyLegacyDaemonWorkspaceOwnership } from "@/workspace/legacy-daemon-workspaces";
import { encodeImages } from "@/utils/encode-images";
import { toErrorMessage } from "@/utils/error-messages";
import {
  resolveComposerAttachmentSubmitFormat,
  splitComposerAttachmentsForSubmit,
} from "@/composer/attachments/submit";
import type {
  CreateAgentRequestOptions,
  DaemonClient,
} from "@getpaseo/client/internal/daemon-client";
import { projectIconPlaceholderLabelFromDisplayName } from "@/utils/project-display-name";
import { requireWorkspaceDirectory } from "@/utils/workspace-directory";
import { navigateToAgent } from "@/utils/navigate-to-agent";
import { navigateToWorkspace } from "@/stores/navigation-active-workspace-store";
import type { MessagePayload } from "@/composer/types";
import {
  createWorkspaceCreationAttempt,
  retryWorkspaceCreationAttempt,
  runWorkspaceCreationAttempt,
  WorkspaceCreationAttemptError,
  type WorkspaceCreationAttempt,
} from "@/screens/workspace-creation-attempt";
function toProjectIconDataUri(icon: { mimeType: string; data: string } | null): string | null {
  if (!icon) {
    return null;
  }
  return `data:${icon.mimeType};base64,${icon.data}`;
}

const SNAP_POINTS: string[] = ["82%", "94%"];

function resolveWorkspaceTitle({
  workspace,
  displayName,
  sourceDirectory,
}: {
  workspace: { name?: string | null; projectDisplayName?: string | null } | null;
  displayName: string;
  sourceDirectory: string;
}): string {
  return (
    workspace?.name ||
    workspace?.projectDisplayName ||
    displayName ||
    sourceDirectory.split(/[\\/]/).findLast(Boolean) ||
    sourceDirectory
  );
}

function buildChatDraftComposerArgs({
  serverId,
  isConnected,
  workspaceDirectory,
  sourceDirectory,
  pendingWorkspaceSetup,
}: {
  serverId: string;
  isConnected: boolean;
  workspaceDirectory: string | undefined;
  sourceDirectory: string;
  pendingWorkspaceSetup: { creationMethod: string } | null;
}) {
  return {
    initialServerId: serverId || null,
    initialValues:
      workspaceDirectory || sourceDirectory
        ? { workingDir: workspaceDirectory || sourceDirectory }
        : undefined,
    isVisible: pendingWorkspaceSetup !== null,
    onlineServerIds: isConnected && serverId ? [serverId] : [],
    lockedWorkingDir: workspaceDirectory || sourceDirectory || undefined,
  };
}

async function callWorkspaceCreation({
  connectedClient,
  attempt,
  retry,
}: {
  connectedClient: DaemonClient;
  attempt: WorkspaceCreationAttempt;
  retry: boolean;
}) {
  if (retry) return retryWorkspaceCreationAttempt({ client: connectedClient, attempt });
  const source = attempt.source;
  // COMPAT(workspaceCreationRetry): preserve first creation on old hosts, never replay it; remove after 2027-03-11.
  const legacyCreate =
    source.kind === "worktree" && !connectedClient.supportsWorkspaceCreationRetry()
      ? (requestId: string) => {
          const { kind: _kind, ...worktreeInput } = source;
          if (!worktreeInput.cwd) throw new Error("A source directory is required for this host.");
          return connectedClient.createPaseoWorktree(
            { ...worktreeInput, cwd: worktreeInput.cwd },
            requestId,
          );
        }
      : undefined;
  return runWorkspaceCreationAttempt({ client: connectedClient, attempt, legacyCreate });
}

function buildCreateAgentOptions({
  composerState,
  text,
  attachments,
  encodedImages,
  workspaceDirectory,
  workspaceId,
  provider,
}: {
  composerState: {
    modeOptions: { id: string }[];
    selectedMode: string;
    effectiveModelId: string | null;
    effectiveThinkingOptionId: string | null;
  };
  text: string;
  attachments: NonNullable<CreateAgentRequestOptions["attachments"]>;
  encodedImages: NonNullable<CreateAgentRequestOptions["images"]> | null;
  workspaceDirectory: string;
  workspaceId: string;
  provider: CreateAgentRequestOptions["provider"];
}): CreateAgentRequestOptions {
  // Reconcile the selected mode against the discovered modes. The mode picker
  // shows modeOptions[0] when the stored mode isn't in the list (e.g. a stale
  // globally-remembered mode this workspace's provider config no longer
  // defines), so the submitted mode must match that display rather than send a
  // stale mode the provider would reject.
  const modeOptionIds = composerState.modeOptions.map((mode) => mode.id);
  const reconciledMode = modeOptionIds.includes(composerState.selectedMode)
    ? composerState.selectedMode
    : (modeOptionIds[0] ?? "");
  return {
    provider,
    cwd: workspaceDirectory,
    workspaceId,
    ...(reconciledMode !== "" ? { modeId: reconciledMode } : {}),
    ...(composerState.effectiveModelId ? { model: composerState.effectiveModelId } : {}),
    ...(composerState.effectiveThinkingOptionId
      ? { thinkingOptionId: composerState.effectiveThinkingOptionId }
      : {}),
    ...(text.trim() ? { initialPrompt: text.trim() } : {}),
    ...(encodedImages && encodedImages.length > 0 ? { images: encodedImages } : {}),
    ...(attachments.length > 0 ? { attachments } : {}),
  };
}

export function WorkspaceSetupDialog() {
  const { t } = useTranslation();
  const toast = useToast();
  const pendingWorkspaceSetup = useWorkspaceSetupStore((state) => state.pendingWorkspaceSetup);
  const clearWorkspaceSetup = useWorkspaceSetupStore((state) => state.clearWorkspaceSetup);
  const mergeWorkspaces = useSessionStore((state) => state.mergeWorkspaces);
  const setHasHydratedWorkspaces = useSessionStore((state) => state.setHasHydratedWorkspaces);
  const setAgents = useSessionStore((state) => state.setAgents);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [createdWorkspace, setCreatedWorkspace] = useState<ReturnType<
    typeof normalizeWorkspaceDescriptor
  > | null>(null);
  const [pendingCreationAttempt, setPendingCreationAttempt] =
    useState<WorkspaceCreationAttempt | null>(null);
  const pendingCreationServerRef = useRef<string | null>(null);
  const creationInFlightRef = useRef(false);
  const creationGenerationRef = useRef(0);
  const submitInFlightRef = useRef(false);
  const pendingSubmissionRef = useRef<MessagePayload | null>(null);
  const [pendingAction, setPendingAction] = useState<"chat" | null>(null);

  const serverId = pendingWorkspaceSetup?.serverId ?? "";
  const supportsForgeSearch = useSessionStore(
    (state) => state.sessions[serverId]?.serverInfo?.features?.forgeSearch === true,
  );
  const sourceDirectory = pendingWorkspaceSetup?.sourceDirectory ?? "";
  const displayName = pendingWorkspaceSetup?.displayName?.trim() ?? "";
  const workspace = createdWorkspace;
  const client = useHostRuntimeClient(serverId);
  const isConnected = useHostRuntimeIsConnected(serverId);
  const chatDraft = useAgentInputDraft({
    draftKey: `workspace-setup:${serverId}:${sourceDirectory}`,
    composer: buildChatDraftComposerArgs({
      serverId,
      isConnected,
      workspaceDirectory: workspace?.workspaceDirectory,
      sourceDirectory,
      pendingWorkspaceSetup,
    }),
  });
  const composerState = chatDraft.composerState;
  if (!composerState && pendingWorkspaceSetup) {
    throw new Error(t("workspaceSetup.errors.composerStateRequired"));
  }

  const { icon: projectIcon } = useProjectIconQuery({
    serverId,
    cwd: sourceDirectory,
  });
  const iconDataUri = toProjectIconDataUri(projectIcon);

  useEffect(() => {
    setErrorMessage(null);
    setCreatedWorkspace(null);
    setPendingAction(null);
    setPendingCreationAttempt(null);
    pendingCreationServerRef.current = null;
    creationGenerationRef.current += 1;
    creationInFlightRef.current = false;
    submitInFlightRef.current = false;
    pendingSubmissionRef.current = null;
  }, [pendingWorkspaceSetup?.creationMethod, serverId, sourceDirectory]);

  const handleClose = useCallback(() => {
    clearWorkspaceSetup();
  }, [clearWorkspaceSetup]);

  const navigateAfterCreation = useCallback(
    (
      workspaceId: string,
      target: { kind: "agent"; agentId: string } | { kind: "terminal"; terminalId: string },
    ) => {
      if (!pendingWorkspaceSetup) {
        return;
      }

      clearWorkspaceSetup();
      if (target.kind === "agent") {
        navigateToAgent({
          serverId: pendingWorkspaceSetup.serverId,
          agentId: target.agentId,
        });
        return;
      }

      navigateToWorkspace({
        serverId: pendingWorkspaceSetup.serverId,
        workspaceId,
        target,
      });
    },
    [clearWorkspaceSetup, pendingWorkspaceSetup],
  );

  const withConnectedClient = useCallback(() => {
    if (!client || !isConnected) {
      throw new Error(t("workspaceSetup.errors.hostDisconnected"));
    }
    return client;
  }, [client, isConnected, t]);
  const ensureWorkspace = useCallback(
    async (input: { cwd: string; attachments: MessagePayload["attachments"] }) => {
      if (!pendingWorkspaceSetup) {
        throw new Error(t("workspaceSetup.errors.pendingRequired"));
      }

      if (createdWorkspace) {
        return createdWorkspace;
      }
      if (creationInFlightRef.current) {
        throw new WorkspaceCreationAttemptError(
          "connection",
          "Workspace creation is already running. Check again once it settles.",
          true,
        );
      }
      if (
        pendingCreationAttempt &&
        pendingCreationServerRef.current !== pendingWorkspaceSetup.serverId
      ) {
        throw new WorkspaceCreationAttemptError(
          "connection",
          "Return to the original host to check the pending workspace creation.",
          true,
        );
      }
      const connectedClient = withConnectedClient();
      const isRetry = pendingCreationAttempt !== null;
      const attempt =
        pendingCreationAttempt ??
        createWorkspaceCreationAttempt(
          pendingWorkspaceSetup.creationMethod === "open_project"
            ? { source: { kind: "directory", path: input.cwd } }
            : {
                source: {
                  kind: "worktree",
                  cwd: input.cwd,
                  worktreeSlug: createNameId(),
                },
              },
        );
      if (!pendingCreationAttempt) {
        pendingCreationServerRef.current = pendingWorkspaceSetup.serverId;
      }
      setPendingCreationAttempt(attempt);
      const generation = creationGenerationRef.current;
      creationInFlightRef.current = true;
      try {
        const payload = await callWorkspaceCreation({
          connectedClient,
          attempt,
          retry: isRetry,
        });

        const normalizedWorkspace = normalizeWorkspaceDescriptor(payload.workspace);
        mergeWorkspaces(pendingWorkspaceSetup.serverId, [normalizedWorkspace]);
        if (pendingWorkspaceSetup.creationMethod === "open_project") {
          setHasHydratedWorkspaces(pendingWorkspaceSetup.serverId, true);
        }
        if (generation === creationGenerationRef.current) {
          setCreatedWorkspace(normalizedWorkspace);
          setPendingCreationAttempt(null);
        }
        return normalizedWorkspace;
      } catch (error) {
        if (
          generation === creationGenerationRef.current &&
          !(error instanceof WorkspaceCreationAttemptError && error.retryable)
        ) {
          setPendingCreationAttempt(null);
        }
        throw error;
      } finally {
        if (generation === creationGenerationRef.current) creationInFlightRef.current = false;
      }
    },
    [
      createdWorkspace,
      mergeWorkspaces,
      pendingCreationAttempt,
      pendingWorkspaceSetup,
      setHasHydratedWorkspaces,
      t,
      withConnectedClient,
    ],
  );

  const getIsStillActive = useCallback(() => {
    const current = useWorkspaceSetupStore.getState().pendingWorkspaceSetup;
    return (
      current?.serverId === pendingWorkspaceSetup?.serverId &&
      current?.sourceDirectory === pendingWorkspaceSetup?.sourceDirectory &&
      current?.creationMethod === pendingWorkspaceSetup?.creationMethod
    );
  }, [
    pendingWorkspaceSetup?.creationMethod,
    pendingWorkspaceSetup?.serverId,
    pendingWorkspaceSetup?.sourceDirectory,
  ]);

  const handleCreateChatAgent = useCallback(
    async (payload: MessagePayload) => {
      if (submitInFlightRef.current) return;
      submitInFlightRef.current = true;
      const generation = creationGenerationRef.current;
      if (pendingCreationAttempt && pendingSubmissionRef.current)
        payload = pendingSubmissionRef.current;
      else pendingSubmissionRef.current = payload;
      const { text, attachments, cwd } = payload;
      try {
        setPendingAction("chat");
        setErrorMessage(null);
        const ensuredWorkspace = await ensureWorkspace({ cwd, attachments });
        if (!getIsStillActive()) return;
        const connectedClient = withConnectedClient();
        if (!composerState) {
          throw new Error(t("workspaceSetup.errors.composerStateRequired"));
        }
        if (!composerState.selectedProvider) {
          throw new Error(t("workspaceSetup.errors.selectModel"));
        }

        const wirePayload = splitComposerAttachmentsForSubmit(attachments, {
          format: resolveComposerAttachmentSubmitFormat({
            supportsForgeAttachments: supportsForgeSearch,
          }),
        });
        const encodedImages = await encodeImages(wirePayload.images);
        const workspaceDirectory = requireWorkspaceDirectory({
          workspaceId: ensuredWorkspace.id,
          workspaceDirectory: ensuredWorkspace.workspaceDirectory,
        });
        const agent = await connectedClient.createAgent(
          buildCreateAgentOptions({
            composerState,
            text,
            attachments: wirePayload.attachments,
            encodedImages: encodedImages ?? null,
            workspaceDirectory,
            workspaceId: ensuredWorkspace.id,
            provider: composerState.selectedProvider,
          }),
        );

        if (!getIsStillActive()) {
          return;
        }

        setAgents(serverId, (previous) => {
          const next = new Map(previous);
          next.set(
            agent.id,
            applyLegacyDaemonWorkspaceOwnership({
              serverId,
              agent: normalizeAgentSnapshot(agent, serverId),
            }),
          );
          return next;
        });
        navigateAfterCreation(ensuredWorkspace.id, { kind: "agent", agentId: agent.id });
      } catch (error) {
        if (!getIsStillActive()) return;
        const message = toErrorMessage(error);
        setErrorMessage(message);
        toast.error(message);
      } finally {
        if (generation === creationGenerationRef.current) submitInFlightRef.current = false;
        if (getIsStillActive()) {
          setPendingAction(null);
        }
      }
    },
    [
      composerState,
      pendingCreationAttempt,
      getIsStillActive,
      navigateAfterCreation,
      serverId,
      setAgents,
      ensureWorkspace,
      t,
      toast,
      withConnectedClient,
      supportsForgeSearch,
    ],
  );

  const handleCheckAgain = useCallback(() => {
    if (pendingSubmissionRef.current) void handleCreateChatAgent(pendingSubmissionRef.current);
  }, [handleCreateChatAgent]);

  const workspaceTitle = resolveWorkspaceTitle({ workspace, displayName, sourceDirectory });

  const placeholderLabel = projectIconPlaceholderLabelFromDisplayName(workspaceTitle);
  const placeholderInitial = placeholderLabel.charAt(0).toUpperCase();

  const isCompact = useIsCompactFormFactor();
  const iconSource = useMemo(() => (iconDataUri ? { uri: iconDataUri } : null), [iconDataUri]);
  const agentControlsWithDisabled = useMemo(
    () =>
      composerState
        ? {
            ...composerState.agentControls,
            disabled: pendingAction !== null,
          }
        : undefined,
    [composerState, pendingAction],
  );

  const composerFooter = useMemo(
    () =>
      isCompact && agentControlsWithDisabled ? (
        <DraftAgentModeControl placement="footer" {...agentControlsWithDisabled} />
      ) : undefined,
    [isCompact, agentControlsWithDisabled],
  );

  const subtitleContent = useMemo(
    () => (
      <View style={styles.subtitleRow}>
        {iconSource ? (
          <Image source={iconSource} style={styles.projectIcon} />
        ) : (
          <View style={styles.projectIconFallback}>
            <Text style={styles.projectIconFallbackText}>{placeholderInitial}</Text>
          </View>
        )}
        <Text style={styles.projectTitle} numberOfLines={1}>
          {workspaceTitle}
        </Text>
      </View>
    ),
    [iconSource, placeholderInitial, workspaceTitle],
  );

  const sheetHeader = useMemo<SheetHeader>(
    () => ({ title: t("workspaceSetup.title"), subtitle: subtitleContent }),
    [subtitleContent, t],
  );

  if (!pendingWorkspaceSetup || !sourceDirectory) {
    return null;
  }

  return (
    <AdaptiveModalSheet
      header={sheetHeader}
      visible={true}
      onClose={handleClose}
      snapPoints={SNAP_POINTS}
      testID="workspace-setup-dialog"
      desktopMaxWidth={640}
    >
      <FileDropZone style={styles.section}>
        <Composer
          agentId={`workspace-setup:${serverId}:${sourceDirectory}`}
          serverId={serverId}
          isPaneFocused={true}
          onSubmitMessage={handleCreateChatAgent}
          isSubmitLoading={pendingAction === "chat"}
          blurOnSubmit={true}
          value={chatDraft.text}
          onChangeText={chatDraft.setText}
          attachments={chatDraft.attachments}
          onChangeAttachments={chatDraft.setAttachments}
          cwd={sourceDirectory}
          clearDraft={chatDraft.clear}
          autoFocus
          commandDraftConfig={composerState?.commandDraftConfig}
          agentControls={agentControlsWithDisabled}
          inputWrapperStyle={styles.composerInputWrapper}
          footer={composerFooter}
        />
      </FileDropZone>

      {errorMessage ? <Text style={styles.errorText}>{errorMessage}</Text> : null}
      <WorkspaceCreationRetryButton
        attempt={pendingCreationAttempt}
        errorMessage={errorMessage}
        created={createdWorkspace !== null}
        disabled={pendingAction !== null}
        onPress={handleCheckAgain}
        label={t("newWorkspace.checkAgain", { defaultValue: "Check again" })}
        testID="workspace-setup-check-again"
      />
    </AdaptiveModalSheet>
  );
}

const styles = StyleSheet.create((theme) => ({
  subtitleRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: theme.spacing[2],
  },
  projectIcon: {
    width: theme.iconSize.md,
    height: theme.iconSize.md,
    borderRadius: theme.borderRadius.sm,
  },
  projectIconFallback: {
    width: theme.iconSize.md,
    height: theme.iconSize.md,
    borderRadius: theme.borderRadius.sm,
    borderWidth: 1,
    borderColor: theme.colors.border,
    alignItems: "center",
    justifyContent: "center",
  },
  projectIconFallbackText: {
    color: theme.colors.foregroundMuted,
    fontSize: 9,
  },
  projectTitle: {
    fontSize: theme.fontSize.sm,
    color: theme.colors.foregroundMuted,
  },
  section: {
    gap: theme.spacing[3],
    marginHorizontal: -theme.spacing[6],
    marginVertical: -theme.spacing[2],
  },
  errorText: {
    fontSize: theme.fontSize.sm,
    color: theme.colors.destructive,
    lineHeight: 20,
  },
  composerInputWrapper: {
    backgroundColor: theme.colors.surface2,
  },
}));
