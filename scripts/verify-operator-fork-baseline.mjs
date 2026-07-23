#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const headArgIndex = process.argv.indexOf("--head");
const head = headArgIndex >= 0 ? process.argv[headArgIndex + 1] : "HEAD";

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

console.log(`Operator fork baseline verified at ${head}.`);
