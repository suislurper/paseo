import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const productionScript = fileURLToPath(new URL("./verify-desktop-bundle.mjs", import.meta.url));

const UI_BUNDLE = ["originDefaultRelation", "patch_equivalent", "archiveUnpushedCommitCount"].join(
  "\n",
);

const SERVER_ASAR = [
  "originDefaultRelation",
  "claude-opus-5",
  "PASEO_AGENT_ARTIFACT_DIR",
  "release_agent_scratch",
  "minimumFreeBytes",
  "collaboration catalog has no ordinary mode",
  "Failed to clean up unused directory workspace after launch failure",
].join("\n");

const CANONICAL_SKILLS = {
  "guide/SKILL.md": "# canonical guide\n",
  "guide/nested/note.md": "nested skill bytes\n",
};

const RESOURCE_TREES = ["linux-unpacked/resources", "win-unpacked/resources"];

function createFixture() {
  const root = mkdtempSync(join(tmpdir(), "paseo-desktop-bundle-"));
  mkdirSync(join(root, "scripts"), { recursive: true });
  copyFileSync(productionScript, join(root, "scripts", "verify-desktop-bundle.mjs"));
  return root;
}

function writeFiles(root, files) {
  for (const [relativePath, contents] of Object.entries(files)) {
    const abs = join(root, relativePath);
    mkdirSync(dirname(abs), { recursive: true });
    writeFileSync(abs, contents);
  }
}

function writeCanonicalSkills(root, files = CANONICAL_SKILLS) {
  writeFiles(
    root,
    Object.fromEntries(
      Object.entries(files).map(([relativePath, contents]) => [`skills/${relativePath}`, contents]),
    ),
  );
}

function writeUiExport(root, prefix) {
  writeFiles(root, {
    [`${prefix}/index.html`]: "<html></html>\n",
    [`${prefix}/_expo/static/js/web/index-test.js`]: UI_BUNDLE,
  });
}

function writeResourcesTree(root, relativeResources, skills = CANONICAL_SKILLS) {
  writeUiExport(root, join("packages/desktop/release", relativeResources, "app-dist"));
  writeFiles(root, {
    [join("packages/desktop/release", relativeResources, "app.asar")]: SERVER_ASAR,
  });
  writeFiles(
    root,
    Object.fromEntries(
      Object.entries(skills).map(([relativePath, contents]) => [
        join("packages/desktop/release", relativeResources, "skills", relativePath),
        contents,
      ]),
    ),
  );
}

function git(root, args) {
  return execFileSync("git", args, {
    cwd: root,
    encoding: "utf8",
    env: {
      ...process.env,
      GIT_CONFIG_GLOBAL: "/dev/null",
      GIT_CONFIG_NOSYSTEM: "1",
      GIT_CONFIG_SYSTEM: "/dev/null",
    },
  });
}

function initGitRepo(root) {
  git(root, ["init"]);
  git(root, ["config", "user.email", "bundle-guard@example.com"]);
  git(root, ["config", "user.name", "Bundle Guard"]);
  git(root, ["add", "-A"]);
  git(root, ["-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture"]);
}

function runGuard(root, mode) {
  return spawnSync(process.execPath, [join(root, "scripts", "verify-desktop-bundle.mjs"), mode], {
    cwd: root,
    encoding: "utf8",
  });
}

function withFixture(fn) {
  const root = createFixture();
  try {
    return fn(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test("--dist accepts a clean checkout with canonical skills and a current UI export", () => {
  withFixture((root) => {
    writeCanonicalSkills(root);
    writeUiExport(root, "packages/app/dist");
    writeFiles(root, {
      "packages/app/src/.keep": "",
      "packages/server/.keep": "",
      "packages/protocol/.keep": "",
    });
    initGitRepo(root);

    const result = runGuard(root, "--dist");
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Desktop UI export verified/);
  });
});

test("--dist fails closed when canonical skills are missing", () => {
  withFixture((root) => {
    writeUiExport(root, "packages/app/dist");
    initGitRepo(root);

    const result = runGuard(root, "--dist");
    assert.equal(result.status, 1);
    assert.match(result.stderr, /canonical skills source is missing/);
  });
});

test("--dist fails closed when canonical skills are empty", () => {
  withFixture((root) => {
    mkdirSync(join(root, "skills"), { recursive: true });
    writeUiExport(root, "packages/app/dist");
    initGitRepo(root);

    const result = runGuard(root, "--dist");
    assert.equal(result.status, 1);
    assert.match(result.stderr, /canonical skills source is empty/);
  });
});

test("--dist fails on a dirty canonical skill and tells the operator to commit owned work", () => {
  withFixture((root) => {
    writeCanonicalSkills(root);
    writeUiExport(root, "packages/app/dist");
    initGitRepo(root);
    writeFileSync(join(root, "skills/guide/SKILL.md"), "# dirty canonical skill\n");

    const result = runGuard(root, "--dist");
    assert.equal(result.status, 1);
    assert.match(result.stderr, /uncommitted changes under .*skills\//);
    assert.match(result.stderr, /skills\/guide\/SKILL\.md/);
    assert.match(result.stderr, /commit owned work so the artifact matches a reviewed commit/);
    assert.doesNotMatch(result.stderr, /stash/i);
  });
});

test("--packaged accepts an exact recursive skills copy on every present resources tree", () => {
  withFixture((root) => {
    writeCanonicalSkills(root);
    for (const tree of RESOURCE_TREES) {
      writeResourcesTree(root, tree);
    }

    const result = runGuard(root, "--packaged");
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Packaged desktop resources verified \(2 tree/);
    assert.match(result.stdout, /skills payload match/);
  });
});

test("--packaged fails when a nested packaged skill is missing on one of two trees", () => {
  withFixture((root) => {
    writeCanonicalSkills(root);
    writeResourcesTree(root, RESOURCE_TREES[0]);
    writeResourcesTree(root, RESOURCE_TREES[1], {
      "guide/SKILL.md": CANONICAL_SKILLS["guide/SKILL.md"],
    });

    const result = runGuard(root, "--packaged");
    assert.equal(result.status, 1);
    assert.match(
      result.stderr,
      /win-unpacked\/resources\/skills\/guide\/nested\/note\.md is missing or unreadable/,
    );
    assert.doesNotMatch(
      result.stderr,
      /linux-unpacked\/resources\/skills\/guide\/nested\/note\.md/,
    );
  });
});

test("--packaged fails when a nested packaged skill mismatches on one of two trees", () => {
  withFixture((root) => {
    writeCanonicalSkills(root);
    writeResourcesTree(root, RESOURCE_TREES[0]);
    writeResourcesTree(root, RESOURCE_TREES[1], {
      ...CANONICAL_SKILLS,
      "guide/nested/note.md": "stale packaged nested skill\n",
    });

    const result = runGuard(root, "--packaged");
    assert.equal(result.status, 1);
    assert.match(
      result.stderr,
      /win-unpacked\/resources\/skills\/guide\/nested\/note\.md does not match canonical skills\/guide\/nested\/note\.md/,
    );
    assert.doesNotMatch(
      result.stderr,
      /linux-unpacked\/resources\/skills\/guide\/nested\/note\.md does not match/,
    );
  });
});

test("--packaged fails closed when required source skills are missing", () => {
  withFixture((root) => {
    for (const tree of RESOURCE_TREES) {
      writeResourcesTree(root, tree);
    }

    const result = runGuard(root, "--packaged");
    assert.equal(result.status, 1);
    assert.match(result.stderr, /canonical skills source is missing/);
  });
});
