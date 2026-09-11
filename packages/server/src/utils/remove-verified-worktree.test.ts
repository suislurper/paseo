import { execFileSync } from "node:child_process";
import { promises as fs } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { removeVerifiedWorktree } from "./remove-verified-worktree.js";
import { getRemotePreservation, withCheckoutWriteLock } from "./worktree-preservation.js";

describe("verified worktree removal", () => {
  let root: string;
  let repo: string;
  let worktree: string;
  let procRoot: string;
  let runtimeDirectory: string;
  let savedHead: string | null;
  const git = (cwd: string, ...args: string[]) =>
    execFileSync("git", args, {
      cwd,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null" },
    }).trim();
  const options = () => ({
    cwd: repo,
    worktree,
    procRoot,
    runtimeDirectory,
    persistHead: async (head: string) => {
      savedHead = head;
    },
    validateReferences: async () => {},
    teardown: async () => {},
  });
  const preserve = () => {
    git(repo, "init", "--bare", join(root, "remote.git"));
    git(repo, "remote", "add", "origin", join(root, "remote.git"));
    git(repo, "push", "origin", "main");
  };
  beforeEach(async () => {
    root = await fs.mkdtemp(join(tmpdir(), "paseo-verified-remove-"));
    repo = join(root, "repo");
    worktree = join(root, "checkout");
    procRoot = join(root, "proc");
    runtimeDirectory = join(root, "runtime");
    savedHead = null;
    await fs.mkdir(repo);
    await fs.mkdir(runtimeDirectory, { mode: 0o700 });
    git(repo, "init", "-b", "main");
    git(repo, "config", "user.name", "Fixture");
    git(repo, "config", "user.email", "fixture@example.invalid");
    await fs.writeFile(join(repo, ".gitignore"), "node_modules/\n.cache/\n");
    git(repo, "add", ".gitignore");
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "fixture");
    git(repo, "worktree", "add", "-b", "feat/task", worktree);
    await fs.mkdir(join(procRoot, "self"), { recursive: true });
    await fs.writeFile(join(procRoot, "self/mountinfo"), "1 0 0:1 / / rw - ext4 /dev/example rw\n");
  });
  afterEach(async () => {
    await fs.rm(root, { recursive: true, force: true });
  });

  it("retains an unpreserved tip, including a deleted remote's stale tracking ref", async () => {
    expect((await getRemotePreservation(worktree)).state).toBe("unpreserved");
    await expect(removeVerifiedWorktree(options())).rejects.toThrow("not verified");
    preserve();
    expect((await getRemotePreservation(worktree)).state).toBe("preserved");
    git(join(root, "remote.git"), "update-ref", "-d", "refs/heads/main");
    await expect(removeVerifiedWorktree(options())).rejects.toThrow("not verified");
    expect(savedHead).toBeNull();
    expect((await fs.stat(worktree)).isDirectory()).toBe(true);
  });

  it("persists recovery before removal and restores the saved tip despite branch-name reuse", async () => {
    preserve();
    await fs.mkdir(join(worktree, "node_modules/pkg"), { recursive: true });
    await fs.writeFile(join(worktree, "node_modules/pkg/index.js"), "export {};\n");
    await expect(
      removeVerifiedWorktree({
        ...options(),
        persistHead: async () => {
          throw new Error("disk full");
        },
      }),
    ).rejects.toThrow("disk full");
    expect((await fs.stat(worktree)).isDirectory()).toBe(true);
    const original = git(worktree, "rev-parse", "HEAD");
    await expect(removeVerifiedWorktree(options())).resolves.toEqual({ branchRemoved: true });
    expect(savedHead).toBe(original);
    await expect(fs.stat(worktree)).rejects.toMatchObject({ code: "ENOENT" });
    expect(git(repo, "branch", "--list", "feat/task")).toBe("");
    await fs.writeFile(join(repo, "later.txt"), "later\n");
    git(repo, "add", "later.txt");
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "later");
    git(repo, "branch", "feat/task");
    git(repo, "worktree", "add", "--no-track", "-b", "restored/task", worktree, savedHead!);
    expect(git(worktree, "rev-parse", "HEAD")).toBe(original);
    expect(git(repo, "rev-parse", "feat/task")).not.toBe(original);
  });

  it("holds a worker-owned checkout before teardown and allows cleanup after release", async () => {
    preserve();
    let teardownRan = false;
    const gitDir = git(worktree, "rev-parse", "--absolute-git-dir");
    await withCheckoutWriteLock(
      gitDir,
      async () => {
        await expect(
          removeVerifiedWorktree({
            ...options(),
            teardown: async () => {
              teardownRan = true;
            },
          }),
        ).rejects.toThrow("worker still owns");
      },
      runtimeDirectory,
    );
    expect(teardownRan).toBe(false);
    expect(savedHead).toBeNull();
    await expect(removeVerifiedWorktree(options())).resolves.toEqual({ branchRemoved: true });
  });

  it("retains files when teardown fails or an active reference appears", async () => {
    preserve();
    await expect(
      removeVerifiedWorktree({
        ...options(),
        teardown: async () => {
          throw new Error("teardown failed");
        },
      }),
    ).rejects.toThrow("teardown failed");
    expect(savedHead).toBeNull();
    let checks = 0;
    await expect(
      removeVerifiedWorktree({
        ...options(),
        validateReferences: async () => {
          if (++checks === 2) throw new Error("active sibling");
        },
      }),
    ).rejects.toThrow("active sibling");
    expect((await fs.stat(worktree)).isDirectory()).toBe(true);
  });

  it("retains new ignored evidence written while recovery metadata is saved", async () => {
    preserve();
    await expect(
      removeVerifiedWorktree({
        ...options(),
        persistHead: async (head) => {
          savedHead = head;
          await fs.mkdir(join(worktree, ".cache"));
          await fs.writeFile(join(worktree, ".cache", "evidence.json"), "{}");
        },
      }),
    ).rejects.toThrow("unknown ignored path");
    expect((await fs.stat(worktree)).isDirectory()).toBe(true);
  });
});
