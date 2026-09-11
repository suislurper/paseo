import { describe, expect, it } from "vitest";
import {
  buildWorktreeArchiveRiskReasons,
  formatOriginDefaultRelationLabel,
  toWorktreeArchiveRisk,
  type OriginDefaultRelation,
  type WorktreeArchiveWarningLabels,
} from "./worktree-archive-warning";

const labels: WorktreeArchiveWarningLabels = {
  title: (name) => `Archive ${name}`,
  confirm: "Archive",
  cancel: "Cancel",
  uncommittedChanges: "Uncommitted changes",
  uncommittedChangesWithDiff: (diff) => `Uncommitted changes: ${diff}`,
  addedLine: (count) => `${count} added`,
  deletedLine: (count) => `${count} deleted`,
  unpreservedCommits: () => "Local commits not preserved remotely",
  preservedNotMerged: "Preserved remotely; not merged",
  preservedMergeUnknown: "Preserved remotely; merge status unknown",
  statusUnknown: "Status unknown",
  includedInOriginDefault: (ref) => `Merged into ${ref}`,
  patchEquivalentToOriginDefault: () => "Changes equivalent to merged work",
};
const relation = (state: OriginDefaultRelation["state"]): OriginDefaultRelation => ({
  state,
  resolvedRef: "origin/master",
  ahead: 12,
  behind: 3,
  uniquePatchCount: 4,
});
const preserved = { state: "preserved" as const, ref: "origin/archive/task", localCommitCount: 0 };
const unpreserved = { state: "unpreserved" as const, ref: null, localCommitCount: 4 };

describe("archive preservation and merge labels", () => {
  it("distinguishes merged, preserved, unpreserved, equivalent, and unknown states", () => {
    expect(formatOriginDefaultRelationLabel(relation("included"), labels, preserved)).toBe(
      "Merged into master",
    );
    expect(formatOriginDefaultRelationLabel(relation("ahead"), labels, preserved)).toBe(
      "Preserved remotely; not merged",
    );
    expect(formatOriginDefaultRelationLabel(relation("ahead"), labels, unpreserved)).toBe(
      "Local commits not preserved remotely",
    );
    expect(
      formatOriginDefaultRelationLabel(
        relation("patch_equivalent_not_included"),
        labels,
        unpreserved,
      ),
    ).toBe("Changes equivalent to merged work");
    expect(formatOriginDefaultRelationLabel(relation("ahead"), labels)).toBe("Status unknown");
  });
  it("does not turn ahead-of-default or missing merge evidence into an unpushed claim", () => {
    expect(
      formatOriginDefaultRelationLabel(relation("diverged_with_unique_commits"), labels, preserved),
    ).toBe("Preserved remotely; not merged");
    expect(formatOriginDefaultRelationLabel(relation("unverifiable"), labels, preserved)).toBe(
      "Preserved remotely; merge status unknown",
    );
    expect(formatOriginDefaultRelationLabel(null, labels, preserved)).toBe(
      "Preserved remotely; merge status unknown",
    );
    expect(formatOriginDefaultRelationLabel(null, labels)).toBe("Status unknown");
  });
  it("suppresses only an exact ordinary default checkout", () => {
    expect(
      formatOriginDefaultRelationLabel(relation("exact"), labels, preserved, "master"),
    ).toBeNull();
    expect(formatOriginDefaultRelationLabel(relation("exact"), labels, preserved, "feature")).toBe(
      "Merged into master",
    );
  });
  it("warns about exact unpreserved history even when changes are patch equivalent", () => {
    expect(
      buildWorktreeArchiveRiskReasons(
        {
          remotePreservation: unpreserved,
          originDefaultRelation: relation("patch_equivalent_not_included"),
        },
        labels,
      ),
    ).toEqual(["Changes equivalent to merged work", "Local commits not preserved remotely"]);
  });
  it("keeps dirty-file warnings independent of preserved commits", () => {
    expect(
      buildWorktreeArchiveRiskReasons(
        { isDirty: true, remotePreservation: preserved, diffStat: { additions: 2, deletions: 1 } },
        labels,
      ),
    ).toEqual(["Uncommitted changes: 2 added, 1 deleted"]);
    expect(buildWorktreeArchiveRiskReasons({}, labels)).toEqual(["Status unknown"]);
  });
  it("carries preservation through sidebar and project archive input", () => {
    expect(
      toWorktreeArchiveRisk({
        archiveRemotePreservation: preserved,
        archiveOriginDefaultRelation: relation("ahead"),
        archiveHasUncommittedChanges: false,
      }),
    ).toMatchObject({
      remotePreservation: preserved,
      originDefaultRelation: relation("ahead"),
      isDirty: false,
    });
  });
});
