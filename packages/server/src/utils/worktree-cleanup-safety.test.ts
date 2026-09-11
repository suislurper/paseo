import { it, expect } from "vitest";
import { promises as fs } from "node:fs";
import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import {
  inspectDisposableCheckout,
  assertNoCheckoutProcesses,
  assertNoCheckoutMounts,
} from "./worktree-cleanup-safety.js";
import { getRemotePreservation, verifyRemotePreservation } from "./worktree-preservation.js";
async function main() {
  const temp = await fs.mkdtemp("/tmp/paseo-cleanup-proof-");
  const repo = temp + "/repo",
    wt = temp + "/checkout",
    proc = temp + "/proc",
    remote = temp + "/remote.git";
  const git = (cwd: string, ...args: string[]) =>
    execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  try {
    await fs.mkdir(repo);
    git(repo, "init", "-b", "main");
    git(repo, "config", "user.name", "Fixture");
    git(repo, "config", "user.email", "fixture@example.invalid");
    await fs.writeFile(repo + "/.gitignore", "node_modules/\nscratch/\n");
    await fs.writeFile(repo + "/README", "fixture\n");
    git(repo, "add", ".gitignore", "README");
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "fixture");
    git(repo, "worktree", "add", "-b", "feat/task", wt);
    await fs.mkdir(proc + "/self", { recursive: true });
    await fs.writeFile(proc + "/self/mountinfo", "1 0 0:1 / / rw - ext4 /dev/example rw\n");
    const first = await inspectDisposableCheckout(wt, { procRoot: proc });
    assert.equal(first.branch, "feat/task");
    assert.equal(
      (await inspectDisposableCheckout(wt, { procRoot: proc })).fingerprint,
      first.fingerprint,
    );
    assert.equal((await getRemotePreservation(wt)).state, "unpreserved");
    await assert.rejects(verifyRemotePreservation(wt, first.head), /not verified/);
    git(repo, "init", "--bare", remote);
    git(repo, "remote", "add", "origin", remote);
    git(repo, "push", "origin", "main");
    assert.equal((await getRemotePreservation(wt)).state, "preserved");
    assert.equal(await verifyRemotePreservation(wt, first.head), "refs/remotes/origin/main");
    await fs.mkdir(wt + "/node_modules/pkg", { recursive: true });
    await fs.writeFile(wt + "/node_modules/pkg/index.js", "export {};\n");
    const cache = await inspectDisposableCheckout(wt, { procRoot: proc });
    assert.notEqual(cache.fingerprint, first.fingerprint);
    await fs.writeFile(wt + "/node_modules/pkg/.env", "fixture-only");
    await assert.rejects(
      inspectDisposableCheckout(wt, { procRoot: proc }),
      /protected ignored path/,
    );
    await fs.unlink(wt + "/node_modules/pkg/.env");
    await fs.mkdir(wt + "/scratch");
    await assert.rejects(inspectDisposableCheckout(wt, { procRoot: proc }), /unknown ignored path/);
    await fs.rmdir(wt + "/scratch");
    await fs.writeFile(
      proc + "/self/mountinfo",
      "2 1 0:1 / " + wt + "/node_modules/pkg/index.js rw - ext4 /dev/example rw\n",
    );
    await assert.rejects(inspectDisposableCheckout(wt, { procRoot: proc }), /mounted path/);
    await fs.writeFile(proc + "/self/mountinfo", "1 0 0:1 / / rw - ext4 /dev/example rw\n");
    await fs.writeFile(wt + "/README", "changed\n");
    await assert.rejects(inspectDisposableCheckout(wt, { procRoot: proc }), /uncommitted/);
    git(wt, "update-index", "--assume-unchanged", "README");
    await assert.rejects(inspectDisposableCheckout(wt, { procRoot: proc }), /hidden index/);
    git(wt, "update-index", "--no-assume-unchanged", "README");
    await fs.writeFile(wt + "/README", "fixture\n");
    await fs.writeFile(wt + "/model.safetensors", "fixture model");
    git(wt, "add", "model.safetensors");
    git(wt, "-c", "commit.gpgsign=false", "commit", "-m", "model fixture");
    await assert.rejects(
      inspectDisposableCheckout(wt, { procRoot: proc }),
      /tracked model or data artifact/,
    );
    await fs.mkdir(proc + "/" + process.pid);
    await fs.writeFile(proc + "/" + process.pid + "/comm", "node");
    await fs.symlink(wt, proc + "/" + process.pid + "/cwd");
    await assert.rejects(assertNoCheckoutProcesses(wt, proc), /still uses/);
    await assert.rejects(
      assertNoCheckoutProcesses(wt, temp + "/missing-proc"),
      /could not be checked/,
    );
  } finally {
    await fs.rm(temp, { recursive: true });
  }
}
it("retains dirty, hidden-index, protected, mounted and process-owned checkouts", main, 30_000);

it.each([
  {
    name: "direct bind source",
    checkout: "/home/fixture/project",
    mounts:
      "1 0 8:1 / / rw - ext4 /dev/a rw\n2 1 8:1 /home/fixture/project /elsewhere rw - ext4 /dev/a rw\n",
  },
  {
    name: "bind source on a separate home filesystem",
    checkout: "/home/fixture/project",
    mounts:
      "1 0 8:1 / / rw - ext4 /dev/a rw\n2 1 8:2 / /home rw - ext4 /dev/b rw\n3 1 8:2 /fixture/project /elsewhere rw - ext4 /dev/b rw\n",
  },
  {
    name: "bind source through a non-root filesystem mount",
    checkout: "/home/fixture/project",
    mounts:
      "1 0 8:1 / / rw - ext4 /dev/a rw\n2 1 8:2 /users /home rw - ext4 /dev/b rw\n3 1 8:2 /users/fixture/project /elsewhere rw - ext4 /dev/b rw\n",
  },
  {
    name: "escaped bind source",
    checkout: "/home/fixture/my project",
    mounts:
      "1 0 8:1 / / rw - ext4 /dev/a rw\n2 1 8:1 /home/fixture/my\\040project /elsewhere rw - ext4 /dev/a rw\n",
  },
  {
    name: "parent bind exposes the checkout",
    checkout: "/home/fixture/project",
    mounts:
      "1 0 8:1 / / rw - ext4 /dev/a rw\n2 1 8:1 /home/fixture /elsewhere rw - ext4 /dev/a rw\n",
  },
  {
    name: "unresolved source",
    checkout: "/home/fixture/project",
    mounts: "1 0 8:1 /other /elsewhere rw - ext4 /dev/a rw\n",
  },
])("retains checkout with $name", async ({ checkout, mounts }) => {
  const proc = await fs.mkdtemp("/tmp/paseo-mount-source-");
  try {
    await fs.mkdir(proc + "/self");
    await fs.writeFile(proc + "/self/mountinfo", mounts);
    await expect(assertNoCheckoutMounts(checkout, proc)).rejects.toThrow(/mount/);
  } finally {
    await fs.rm(proc, { recursive: true });
  }
});

it("allows unrelated same-filesystem bind mounts", async () => {
  const proc = await fs.mkdtemp("/tmp/paseo-unrelated-mount-");
  try {
    await fs.mkdir(proc + "/self");
    await fs.writeFile(
      proc + "/self/mountinfo",
      "1 0 8:1 / / rw - ext4 /dev/a rw\n2 1 8:1 /other /elsewhere rw - ext4 /dev/a rw\n",
    );
    await expect(assertNoCheckoutMounts("/home/fixture/project", proc)).resolves.toBeUndefined();
  } finally {
    await fs.rm(proc, { recursive: true });
  }
});
