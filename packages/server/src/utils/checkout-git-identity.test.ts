import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, test, vi } from "vitest";
import { getCheckoutIdentity } from "./checkout-git.js";
import { runGitCommand } from "./run-git-command.js";
import { createWorktree } from "./worktree.js";

vi.mock("./run-git-command.js", async (importOriginal) => {
  const original = await importOriginal<typeof import("./run-git-command.js")>();
  return { ...original, runGitCommand: vi.fn(original.runGitCommand) };
});

const roots: string[] = [];

function createRepository() {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "paseo-checkout-identity-")));
  roots.push(root);
  const repo = join(root, "repo");
  execFileSync("git", ["init", "-b", "main", repo], { stdio: "pipe" });
  execFileSync(
    "git",
    [
      "-c",
      "user.name=Test",
      "-c",
      "user.email=test@example.com",
      "-c",
      "commit.gpgsign=false",
      "commit",
      "--allow-empty",
      "-m",
      "initial",
    ],
    { cwd: repo, stdio: "pipe" },
  );
  return { root, repo };
}

afterEach(() => {
  vi.clearAllMocks();
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

test("reads fresh nested checkout identity without status, history or remote access", async () => {
  const { repo } = createRepository();
  const nested = join(repo, "nested");
  mkdirSync(nested);
  execFileSync("git", ["remote", "add", "origin", "https://example.invalid/repo.git"], {
    cwd: repo,
  });
  await expect(getCheckoutIdentity(nested)).resolves.toEqual({
    isGit: true,
    repoRoot: repo,
    currentBranch: "main",
    mainRepoRoot: null,
    remoteUrl: "https://example.invalid/repo.git",
    isPaseoOwnedWorktree: false,
  });
  execFileSync("git", ["checkout", "-b", "next"], { cwd: repo, stdio: "pipe" });
  await expect(getCheckoutIdentity(nested)).resolves.toMatchObject({ currentBranch: "next" });
  const commands = vi.mocked(runGitCommand).mock.calls.map(([args]) => args[0]);
  expect(commands.length).toBeGreaterThan(0);
  expect(commands.every((command) => ["rev-parse", "config", "worktree"].includes(command))).toBe(
    true,
  );
});

test("preserves linked checkout main repository identity and detached HEAD", async () => {
  const { root, repo } = createRepository();
  const linked = join(root, "linked");
  execFileSync("git", ["worktree", "add", "--detach", linked, "HEAD"], {
    cwd: repo,
    stdio: "pipe",
  });
  await expect(getCheckoutIdentity(linked)).resolves.toEqual({
    isGit: true,
    repoRoot: linked,
    currentBranch: null,
    mainRepoRoot: repo,
    remoteUrl: null,
    isPaseoOwnedWorktree: false,
  });
});

test("recognizes a non-Git directory", async () => {
  const { root } = createRepository();
  await expect(getCheckoutIdentity(root)).resolves.toEqual({ isGit: false });
});

test("preserves Paseo-owned worktree identity", async () => {
  const { root, repo } = createRepository();
  const paseoHome = join(root, "paseo-home");
  const worktree = await createWorktree({
    cwd: repo,
    paseoHome,
    worktreeSlug: "identity-check",
    runSetup: false,
    source: { kind: "branch-off", baseBranch: "main", branchName: "identity-check" },
  });
  await expect(getCheckoutIdentity(worktree.worktreePath, { paseoHome })).resolves.toEqual({
    isGit: true,
    repoRoot: worktree.worktreePath,
    currentBranch: "identity-check",
    mainRepoRoot: repo,
    remoteUrl: null,
    isPaseoOwnedWorktree: true,
  });
});
