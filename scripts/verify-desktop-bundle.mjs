#!/usr/bin/env node

// Fail-closed packaging check for the desktop artifact.
//
// The AppImage ships two independently built halves plus a skills payload:
//   - resources/app.asar   <- packages/server/dist (daemon, model manifests)
//   - resources/app-dist   <- packages/app/dist    (Expo web export, the UI)
//   - resources/skills     <- skills/              (canonical agent instructions)
//
// `npm run build --workspace=@getpaseo/desktop` rebuilds only the first one and
// silently packages whatever `packages/app/dist` happens to contain. On
// 2026-07-27 that shipped a 15-day-old UI bundle: Opus 5 appeared in the picker
// (server side) while the landed/unpushed workspace sidebar labels vanished (UI
// side), with no build error anywhere. This guard makes that state impossible to
// package.
//
// Modes:
//   --dist       verify packages/app/dist before it is packaged
//   --packaged   verify release/linux-unpacked/resources after electron-builder
//
// See docs/operator-fork.md "Build invariant".

import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// One marker per guarded operator feature, mirroring the requiredHistory list in
// verify-operator-fork-baseline.mjs. A marker is any string the feature's source
// forces into the built bundle (a protocol field name, a discriminant literal).
// When you add a guarded feature, add its marker here in the same change.
const UI_MARKERS = [
  ["originDefaultRelation", "landed-vs-unpushed workspace git relation (1bf93bb7e)"],
  ["patch_equivalent", "patch-equivalent archive classification (1bf93bb7e)"],
  ["archiveRemotePreservation", "remote preservation on sidebar rows"],
  ["workspaceCreationRetry", "capability-gated workspace creation retries"],
  ["archivedFilesRetained", "truthful archive cleanup outcomes"],
];

const SERVER_MARKERS = [
  ["workspaceSafeCleanup", "verified checkout cleanup capability"],
  ["workspaceCreationRetry", "durable workspace creation identity"],
  ["archivedHead", "exact-commit checkout recovery"],
  ["originDefaultRelation", "origin-default relation computed server side (1bf93bb7e)"],
  ["claude-opus-5", "Opus 5 Claude model manifest entries (3778a52d9)"],
  ["PASEO_AGENT_ARTIFACT_DIR", "managed per-agent runtime storage (95c63c3c5)"],
  ["release_agent_scratch", "explicit scratch-release receipt (95c63c3c5)"],
  ["minimumFreeBytes", "disk-pressure worktree admission (95c63c3c5)"],
  ["collaboration catalog has no ordinary mode", "ordinary Codex mode catalog safety (827eb7861)"],
  [
    "Failed to clean up unused directory workspace after launch failure",
    "failed directory launch cleanup (2089c093e)",
  ],
];

// Sources whose changes must be reflected in the Expo export before packaging.
const UI_SOURCE_PATHS = ["packages/app/src", "packages/app/app.config.js", "packages/app/index.ts"];

function fail(lines) {
  console.error("Refusing operator Paseo package: desktop bundle verification failed.");
  for (const line of lines) console.error(`- ${line}`);
  console.error("");
  console.error("Rebuild with `npm run build:desktop` from the repo root. That is the only");
  console.error("supported packaging path: it runs the Expo web export that produces the UI");
  console.error(
    "half of the AppImage. Never package via `npm run build --workspace=@getpaseo/desktop`.",
  );
  process.exit(1);
}

function newestMtimeMs(absPath) {
  let newest = 0;
  const walk = (target) => {
    let stats;
    try {
      stats = statSync(target);
    } catch {
      return;
    }
    if (stats.isDirectory()) {
      for (const entry of readdirSync(target)) {
        if (entry === "node_modules" || entry.startsWith(".")) continue;
        walk(join(target, entry));
      }
      return;
    }
    if (stats.mtimeMs > newest) newest = stats.mtimeMs;
  };
  walk(absPath);
  return newest;
}

function findWebBundles(distRoot) {
  const webDir = join(distRoot, "_expo", "static", "js", "web");
  let entries;
  try {
    entries = readdirSync(webDir);
  } catch {
    return [];
  }
  return entries
    .filter((name) => name.startsWith("index-") && name.endsWith(".js"))
    .map((name) => join(webDir, name));
}

function checkMarkers(filePath, markers, label, problems) {
  let contents;
  try {
    contents = readFileSync(filePath, "utf8");
  } catch (error) {
    problems.push(`${label}: unreadable (${error instanceof Error ? error.message : error})`);
    return;
  }
  for (const [marker, feature] of markers) {
    if (!contents.includes(marker)) {
      problems.push(`${label}: missing "${marker}" — ${feature}`);
    }
  }
}

function describeFsError(error) {
  return error instanceof Error ? error.message : error;
}

function listRelativeFiles(root) {
  const files = [];
  const walk = (absDir, relDir) => {
    let entries;
    try {
      entries = readdirSync(absDir, { withFileTypes: true });
    } catch (error) {
      throw new Error(`${relDir || "."}: unreadable (${describeFsError(error)})`, { cause: error });
    }
    for (const entry of entries) {
      if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
      const childRel = relDir ? `${relDir}/${entry.name}` : entry.name;
      const childAbs = join(absDir, entry.name);
      if (entry.isDirectory()) {
        walk(childAbs, childRel);
      } else if (entry.isFile()) {
        files.push(childRel);
      }
    }
  };
  walk(root, "");
  files.sort();
  return files;
}

function canonicalSkillsRoot() {
  return join(repoRoot, "skills");
}

function joinSkillPath(root, relativePath) {
  return join(root, ...relativePath.split("/"));
}

function loadCanonicalSkillInventory(problems) {
  const skillsRoot = canonicalSkillsRoot();
  try {
    if (!statSync(skillsRoot).isDirectory()) {
      problems.push("skills/: canonical skills source is not a directory.");
      return [];
    }
  } catch (error) {
    problems.push(`skills/: canonical skills source is missing (${describeFsError(error)}).`);
    return [];
  }

  let files;
  try {
    files = listRelativeFiles(skillsRoot);
  } catch (error) {
    problems.push(`skills/: canonical skills source is unreadable (${describeFsError(error)}).`);
    return [];
  }
  if (files.length === 0) {
    problems.push("skills/: canonical skills source is empty.");
    return [];
  }
  return files;
}

function verifyPackagedSkills(resources, label, sourceFiles, problems) {
  const packagedRoot = join(resources, "skills");
  for (const relativePath of sourceFiles) {
    const sourcePath = joinSkillPath(canonicalSkillsRoot(), relativePath);
    const packagedPath = joinSkillPath(packagedRoot, relativePath);
    let source;
    try {
      source = readFileSync(sourcePath);
    } catch (error) {
      problems.push(
        `${label}: canonical skill ${relativePath} is unreadable (${describeFsError(error)}).`,
      );
      continue;
    }
    let packaged;
    try {
      packaged = readFileSync(packagedPath);
    } catch (error) {
      problems.push(
        `${label}/skills/${relativePath} is missing or unreadable (${describeFsError(error)}).`,
      );
      continue;
    }
    if (!source.equals(packaged)) {
      problems.push(
        `${label}/skills/${relativePath} does not match canonical skills/${relativePath}.`,
      );
    }
  }
}

function verifyDist() {
  const problems = [];
  loadCanonicalSkillInventory(problems);
  const distRoot = join(repoRoot, "packages", "app", "dist");

  let distStats;
  try {
    distStats = statSync(join(distRoot, "index.html"));
  } catch {
    fail(["packages/app/dist/index.html is missing — the Expo web export never ran or was wiped."]);
    return;
  }

  const bundles = findWebBundles(distRoot);
  if (bundles.length === 0) {
    fail(["packages/app/dist has no _expo/static/js/web/index-*.js bundle."]);
    return;
  }

  // Markers must appear in at least one bundle; Expo may split the entry chunk.
  const combined = bundles.map((bundle) => readFileSync(bundle, "utf8")).join("\n");
  for (const [marker, feature] of UI_MARKERS) {
    if (!combined.includes(marker)) {
      problems.push(`packages/app/dist bundle: missing "${marker}" — ${feature}`);
    }
  }

  const bundleMtimeMs = Math.max(distStats.mtimeMs, ...bundles.map((b) => statSync(b).mtimeMs));
  for (const relativePath of UI_SOURCE_PATHS) {
    const sourceMtimeMs = newestMtimeMs(join(repoRoot, relativePath));
    if (sourceMtimeMs > bundleMtimeMs) {
      problems.push(
        `${relativePath} changed after the Expo export (source ${new Date(sourceMtimeMs).toISOString()} > bundle ${new Date(bundleMtimeMs).toISOString()}).`,
      );
    }
  }

  if (problems.length > 0) fail(problems);
  console.log(
    `Desktop UI export verified (${bundles.length} bundle(s), built ${new Date(bundleMtimeMs).toISOString()}).`,
  );
}

// electron-builder lays the resources dir out per target platform. Verify every
// tree this run actually produced, so the same check works on the operator's
// Linux box and on the macOS/Windows release runners.
const PACKAGED_RESOURCE_DIRS = [
  "linux-unpacked/resources",
  "win-unpacked/resources",
  "mac/Paseo.app/Contents/Resources",
  "mac-arm64/Paseo.app/Contents/Resources",
];

function verifyPackaged() {
  const problems = [];
  const releaseRoot = join(repoRoot, "packages", "desktop", "release");
  const sourceFiles = loadCanonicalSkillInventory(problems);

  const present = PACKAGED_RESOURCE_DIRS.map((relativePath) =>
    join(releaseRoot, relativePath),
  ).filter((candidate) => {
    try {
      return statSync(candidate).isDirectory();
    } catch {
      return false;
    }
  });

  if (present.length === 0) {
    fail([
      `no packaged resources tree under ${releaseRoot}`,
      `looked for: ${PACKAGED_RESOURCE_DIRS.join(", ")}`,
    ]);
    return;
  }

  for (const resources of present) {
    const label = resources.slice(releaseRoot.length + 1);
    const bundles = findWebBundles(join(resources, "app-dist"));
    if (bundles.length === 0) {
      problems.push(`${label}/app-dist has no _expo/static/js/web/index-*.js bundle.`);
    } else {
      const combined = bundles.map((bundle) => readFileSync(bundle, "utf8")).join("\n");
      for (const [marker, feature] of UI_MARKERS) {
        if (!combined.includes(marker)) {
          problems.push(`${label} UI bundle: missing "${marker}" — ${feature}`);
        }
      }
    }

    checkMarkers(join(resources, "app.asar"), SERVER_MARKERS, `${label}/app.asar`, problems);
    if (sourceFiles.length > 0) {
      verifyPackagedSkills(resources, label, sourceFiles, problems);
    }
  }

  if (problems.length > 0) fail(problems);
  console.log(
    `Packaged desktop resources verified (${present.length} tree(s); UI export, server asar, and skills payload match).`,
  );
}

function verifyGitCleanliness() {
  // A packaged artifact must be reproducible from the reviewed commit. Uncommitted
  // work in the app, server, protocol, or canonical skills means the AppImage
  // does not match any SHA. electron-builder copies skills/ into resources/skills.
  const dirty = execFileSync(
    "git",
    [
      "status",
      "--porcelain",
      "--",
      "packages/app",
      "packages/server",
      "packages/protocol",
      "skills",
    ],
    { cwd: repoRoot, encoding: "utf8" },
  ).trim();
  if (dirty) {
    fail([
      "uncommitted changes under packages/app, packages/server, packages/protocol or skills/:",
      ...dirty.split(/\r?\n/).map((line) => `    ${line}`),
      "commit owned work so the artifact matches a reviewed commit.",
    ]);
  }
}

const mode = process.argv[2];
if (mode === "--dist") {
  verifyGitCleanliness();
  verifyDist();
} else if (mode === "--packaged") {
  verifyPackaged();
} else {
  console.error("Usage: node scripts/verify-desktop-bundle.mjs --dist | --packaged");
  process.exit(2);
}
