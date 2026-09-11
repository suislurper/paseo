import type { RemotePreservation } from "@getpaseo/protocol/messages";
import { confirmDialog } from "@/utils/confirm-dialog";
import { i18n } from "@/i18n/i18next";

export type OriginDefaultRelationState =
  | "exact"
  | "included"
  | "ahead"
  | "patch_equivalent_not_included"
  | "diverged_with_unique_commits"
  | "unverifiable";

export interface OriginDefaultRelation {
  state: OriginDefaultRelationState;
  resolvedRef: string | null;
  ahead: number | null;
  behind: number | null;
  uniquePatchCount: number | null;
}

export interface WorktreeArchiveRisk {
  isDirty?: boolean | null;
  remotePreservation?: RemotePreservation | null;
  originDefaultRelation?: OriginDefaultRelation | null;
  diffStat?: { additions: number; deletions: number } | null;
}

export interface WorktreeArchiveRiskInput {
  archiveHasUncommittedChanges?: boolean | null;
  archiveRemotePreservation?: RemotePreservation | null;
  archiveOriginDefaultRelation?: OriginDefaultRelation | null;
  diffStat?: WorktreeArchiveRisk["diffStat"];
}

export interface WorktreeArchiveConfirmationInput extends WorktreeArchiveRisk {
  workspaceName: string;
}

export interface WorktreeArchiveWarningLabels {
  title: (workspaceName: string) => string;
  confirm: string;
  cancel: string;
  uncommittedChanges: string;
  uncommittedChangesWithDiff: (diffStat: string) => string;
  addedLine: (count: number) => string;
  deletedLine: (count: number) => string;
  unpreservedCommits: (count: number | null) => string;
  preservedNotMerged: string;
  preservedMergeUnknown: string;
  statusUnknown: string;
  includedInOriginDefault: (resolvedRef: string) => string;
  patchEquivalentToOriginDefault: (resolvedRef: string) => string;
}

function defaultResolvedRefLabel(resolvedRef: string | null | undefined): string {
  return resolvedRef && resolvedRef.length > 0 ? resolvedRef : "origin/default";
}

export const DEFAULT_WORKTREE_ARCHIVE_WARNING_LABELS: WorktreeArchiveWarningLabels = {
  title: (workspaceName) => i18n.t("workspace.git.actions.archiveWarning.title", { workspaceName }),
  confirm: i18n.t("workspace.git.actions.archiveWarning.confirm"),
  cancel: i18n.t("workspace.git.actions.archiveWarning.cancel"),
  uncommittedChanges: i18n.t("workspace.git.actions.archiveWarning.uncommittedChanges"),
  uncommittedChangesWithDiff: (diffStat) =>
    i18n.t("workspace.git.actions.archiveWarning.uncommittedChangesWithDiff", { diffStat }),
  addedLine: (count) =>
    count === 1
      ? i18n.t("workspace.git.actions.archiveWarning.addedLine", { count })
      : i18n.t("workspace.git.actions.archiveWarning.addedLines", { count }),
  deletedLine: (count) =>
    count === 1
      ? i18n.t("workspace.git.actions.archiveWarning.deletedLine", { count })
      : i18n.t("workspace.git.actions.archiveWarning.deletedLines", { count }),
  unpreservedCommits: (count) =>
    i18n.t("workspace.git.actions.archiveWarning.unpreservedCommits", { count }),
  preservedNotMerged: i18n.t("workspace.git.actions.archiveWarning.preservedNotMerged"),
  preservedMergeUnknown: i18n.t("workspace.git.actions.archiveWarning.preservedMergeUnknown"),
  statusUnknown: i18n.t("workspace.git.actions.archiveWarning.statusUnknown"),
  includedInOriginDefault: (resolvedRef) =>
    i18n.t("workspace.git.actions.archiveWarning.mergedIntoDefault", { resolvedRef }),
  patchEquivalentToOriginDefault: (resolvedRef) =>
    i18n.t("workspace.git.actions.archiveWarning.equivalentToMerged", { resolvedRef }),
};

export function toWorktreeArchiveRisk(input: WorktreeArchiveRiskInput): WorktreeArchiveRisk {
  return {
    isDirty: input.archiveHasUncommittedChanges,
    remotePreservation: input.archiveRemotePreservation,
    originDefaultRelation: input.archiveOriginDefaultRelation,
    diffStat: input.diffStat,
  };
}

function formatDiffStat(
  diffStat: WorktreeArchiveRisk["diffStat"],
  labels: WorktreeArchiveWarningLabels,
): string | null {
  if (!diffStat) {
    return null;
  }

  const parts: string[] = [];
  if (diffStat.additions > 0) {
    parts.push(labels.addedLine(diffStat.additions));
  }
  if (diffStat.deletions > 0) {
    parts.push(labels.deletedLine(diffStat.deletions));
  }

  return parts.length > 0 ? parts.join(", ") : null;
}

export function buildWorktreeArchiveRiskReasons(
  input: WorktreeArchiveRisk,
  labels: WorktreeArchiveWarningLabels = DEFAULT_WORKTREE_ARCHIVE_WARNING_LABELS,
): string[] {
  const reasons: string[] = [];
  const diffStat = input.diffStat;
  const hasDiffStatChanges = diffStat ? diffStat.additions > 0 || diffStat.deletions > 0 : false;
  const hasUncommittedChanges =
    input.isDirty === true || (input.isDirty == null && hasDiffStatChanges);

  if (hasUncommittedChanges) {
    const diffStatLabel = formatDiffStat(diffStat, labels);
    reasons.push(
      diffStatLabel ? labels.uncommittedChangesWithDiff(diffStatLabel) : labels.uncommittedChanges,
    );
  }

  const relation = input.originDefaultRelation;
  if (relation?.state === "patch_equivalent_not_included") {
    reasons.push(
      labels.patchEquivalentToOriginDefault(defaultResolvedRefLabel(relation.resolvedRef)),
    );
  }
  // Patch equivalence and being ahead of the default branch are not proof that
  // the exact commit is preserved. Cleanup independently verifies remote history.
  if (input.remotePreservation?.state === "unpreserved") {
    reasons.push(labels.unpreservedCommits(input.remotePreservation.localCommitCount));
  } else if (!input.remotePreservation || input.remotePreservation.state === "unknown") {
    reasons.push(labels.statusUnknown);
  }

  return reasons;
}

/**
 * Local branch name for the origin-default tip, e.g. `origin/main` → `main`.
 * Returns null when the resolved ref is missing or not an origin/* short ref.
 */
function originDefaultBranchName(resolvedRef: string | null | undefined): string | null {
  if (!resolvedRef || resolvedRef.length === 0) {
    return null;
  }
  if (resolvedRef.startsWith("origin/")) {
    const name = resolvedRef.slice("origin/".length);
    return name.length > 0 ? name : null;
  }
  if (resolvedRef.startsWith("refs/remotes/origin/")) {
    const name = resolvedRef.slice("refs/remotes/origin/".length);
    return name.length > 0 ? name : null;
  }
  return null;
}

/**
 * Ordinary default-branch checkout at the tip: "Included in origin/main" while
 * already on `main` is a tautology. Scope is display-only — classification and
 * archive safety are unchanged.
 */
function isOrdinaryExactDefaultCheckout(
  relation: OriginDefaultRelation,
  currentBranch: string | null | undefined,
): boolean {
  if (relation.state !== "exact") {
    return false;
  }
  if (!currentBranch) {
    return false;
  }
  const defaultBranch = originDefaultBranchName(relation.resolvedRef);
  return defaultBranch !== null && currentBranch === defaultBranch;
}

/** Status/label helper for sidebar and fallbacks — not used as archive land authority. */
export function formatOriginDefaultRelationLabel(
  relation: OriginDefaultRelation | null | undefined,
  labels: WorktreeArchiveWarningLabels = DEFAULT_WORKTREE_ARCHIVE_WARNING_LABELS,
  remotePreservation?: RemotePreservation | null,
  currentBranch?: string | null,
): string | null {
  if (relation?.state === "exact" && isOrdinaryExactDefaultCheckout(relation, currentBranch))
    return null;
  if (relation?.state === "exact" || relation?.state === "included") {
    return labels.includedInOriginDefault(
      originDefaultBranchName(relation.resolvedRef) ??
        defaultResolvedRefLabel(relation.resolvedRef),
    );
  }
  if (relation?.state === "patch_equivalent_not_included") {
    return labels.patchEquivalentToOriginDefault(defaultResolvedRefLabel(relation.resolvedRef));
  }
  if (remotePreservation?.state === "unpreserved") {
    return labels.unpreservedCommits(remotePreservation.localCommitCount);
  }
  if (remotePreservation?.state === "preserved") {
    return relation && relation.state !== "unverifiable"
      ? labels.preservedNotMerged
      : labels.preservedMergeUnknown;
  }
  return labels.statusUnknown;
}

export function buildWorktreeArchiveConfirmationMessage(
  input: WorktreeArchiveConfirmationInput,
  labels: WorktreeArchiveWarningLabels = DEFAULT_WORKTREE_ARCHIVE_WARNING_LABELS,
): string | null {
  const reasons = buildWorktreeArchiveRiskReasons(input, labels);
  if (reasons.length === 0) {
    return null;
  }

  return reasons.join("\n");
}

export async function confirmRiskyWorktreeArchive(
  input: WorktreeArchiveConfirmationInput,
  labels: WorktreeArchiveWarningLabels = DEFAULT_WORKTREE_ARCHIVE_WARNING_LABELS,
): Promise<boolean> {
  const message = buildWorktreeArchiveConfirmationMessage(input, labels);
  if (!message) {
    return true;
  }

  return await confirmDialog({
    title: labels.title(input.workspaceName),
    message,
    confirmLabel: labels.confirm,
    cancelLabel: labels.cancel,
    destructive: true,
  });
}
