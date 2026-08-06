#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const headArgIndex = process.argv.indexOf("--head");
const hasExplicitHead = headArgIndex >= 0;
const head = headArgIndex >= 0 ? process.argv[headArgIndex + 1] : "HEAD";
const canonicalRepository = "github.com/suislurper/paseo";

if (!head) {
  console.error("Missing value for --head");
  process.exit(2);
}

const requiredHistory = [
  ["70c329efcc7d9d128df5c6712a04f503a28aa12e", "archive cleanup and live-chat fixes"],
  ["39486271a", "per-profile provider usage"],
  ["4a1eb23a7", "Claude permission-safe control plane"],
  ["dfa1b281b", "per-profile Claude history"],
  ["114f9448d", "Claude account profile switching"],
  ["19b82f0c9", "base-provider retention for custom profiles"],
  ["26befb70c", "desktop active-provider selection"],
  [
    "72e5e6ecb2674bc6662d13bb53e6e73f140f6089",
    "reviewed provider switching, compatibility, and forced usage refresh fixes",
  ],
  [
    "ef189b45315da7cc1aa515c0f697b1eadb7ccca2",
    "landed-to-default sidebar status and fail-closed archive safety",
  ],
  [
    "36f08fff357f5bae0e07c5acd21d11ef48244a09",
    "final deletion-boundary safety and exact canonical packaging enforcement",
  ],
  ["3778a52d9292e6d9e67f56b025464bff3447b5a1", "Opus 5 Claude model manifest entries"],
  [
    "ca0d84741b23d5eadb7a040bf76f3f4ec9a2c96d",
    "fail-closed desktop UI export verification (stale app-dist cannot be packaged)",
  ],
  [
    "1b668a654a37c43d8626b34eb59cc732751a2c9a",
    "server-projected bounded schedule identity inspection",
  ],
  [
    "95c63c3c5b3b9cf56f66a17954a782cbbafb508e",
    "managed agent runtime storage, fail-closed cleanup probes, and disk admission",
  ],
];

function isAncestor(commit, descendant) {
  try {
    execFileSync("git", ["merge-base", "--is-ancestor", commit, descendant], {
      cwd: repoRoot,
      stdio: "ignore",
    });
    return true;
  } catch {
    return false;
  }
}

function git(args) {
  return execFileSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

function isCanonicalRemoteUrl(url) {
  const normalized = url
    .trim()
    .replace(/\.git$/i, "")
    .replace(/^ssh:\/\/git@/i, "");
  return (
    normalized.toLowerCase() === `https://${canonicalRepository}` ||
    normalized.toLowerCase() === `git@github.com:suislurper/paseo` ||
    normalized.toLowerCase() === canonicalRepository
  );
}

function verifyCanonicalPackagingCheckout() {
  const branch = git(["branch", "--show-current"]);
  if (branch !== "main") {
    throw new Error(
      `packaging requires branch 'main'; current branch is '${branch || "<detached>"}'`,
    );
  }

  const remotes = git(["remote"]).split(/\r?\n/).filter(Boolean);
  const canonicalRemote = remotes.find((remote) =>
    isCanonicalRemoteUrl(git(["remote", "get-url", remote])),
  );
  if (!canonicalRemote) {
    throw new Error(`no Git remote points to ${canonicalRepository}`);
  }

  const canonicalMainRef = `refs/remotes/${canonicalRemote}/main`;
  let canonicalMain;
  try {
    canonicalMain = git(["rev-parse", "--verify", canonicalMainRef]);
  } catch {
    throw new Error(`missing ${canonicalMainRef}; fetch ${canonicalRemote} main before packaging`);
  }
  const buildHead = git(["rev-parse", "HEAD"]);
  if (buildHead !== canonicalMain) {
    throw new Error(
      `HEAD ${buildHead} does not equal ${canonicalRemote}/main ${canonicalMain}; push and verify canonical main before packaging`,
    );
  }
}

const missing = requiredHistory.filter(([commit]) => !isAncestor(commit, head));
if (missing.length > 0) {
  console.error(`Refusing operator Paseo build from ${head}: required fork history is missing.`);
  for (const [commit, feature] of missing) {
    console.error(`- ${commit}: ${feature}`);
  }
  console.error(
    "Build from suislurper/paseo main, or intentionally port and update this guard with reviewed replacement commits.",
  );
  process.exit(1);
}

if (!hasExplicitHead) {
  try {
    verifyCanonicalPackagingCheckout();
  } catch (error) {
    console.error(
      `Refusing operator Paseo package from this checkout: ${
        error instanceof Error ? error.message : String(error)
      }.`,
    );
    console.error(
      "Use suislurper/paseo main at the exact fetched remote-main commit. Use --head only for ancestry review/testing.",
    );
    process.exit(1);
  }
}

console.log(
  hasExplicitHead
    ? `Operator fork ancestry verified at ${head} (review/testing mode).`
    : `Canonical operator fork packaging checkout verified at ${head}.`,
);
