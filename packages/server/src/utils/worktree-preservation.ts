import type { RemotePreservation } from "@getpaseo/protocol/messages";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { constants, promises as fs } from "node:fs";
import { join, resolve } from "node:path";
import { runGitCommand } from "./run-git-command.js";

export class WorktreeCleanupHold extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WorktreeCleanupHold";
  }
}

interface PreservingRef {
  ref: string;
  head: string;
}

const objectId = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;

async function preservingRefs(cwd: string, head = "HEAD"): Promise<PreservingRef[]> {
  const result = await runGitCommand(
    [
      "for-each-ref",
      "--contains=" + head,
      "--format=%(refname) %(objectname) %(symref)",
      "refs/remotes/",
    ],
    { cwd },
  );
  if (result.truncated) throw new WorktreeCleanupHold("Remote history could not be inspected.");
  return result.stdout.split("\n").flatMap((line) => {
    const [ref, sha, symbolic] = line.trim().split(/\s+/);
    return ref?.startsWith("refs/remotes/") && objectId.test(sha ?? "") && !symbolic
      ? [{ ref, head: sha! }]
      : [];
  });
}

/** Display evidence from fetched refs. Destructive callers must verify it live. */
export async function getRemotePreservation(cwd: string): Promise<RemotePreservation> {
  try {
    const refs = await preservingRefs(cwd);
    if (refs.length > 0) {
      return {
        state: "preserved",
        ref: refs[0]!.ref.replace("refs/remotes/", ""),
        localCommitCount: 0,
      };
    }
    const count = await runGitCommand(["rev-list", "--count", "HEAD", "--not", "--remotes"], {
      cwd,
    });
    const value = Number(count.stdout.trim());
    if (
      count.truncated ||
      !/^\d+$/.test(count.stdout.trim()) ||
      !Number.isSafeInteger(value) ||
      value < 1
    ) {
      return { state: "unknown", ref: null, localCommitCount: null };
    }
    return { state: "unpreserved", ref: null, localCommitCount: value };
  } catch {
    return { state: "unknown", ref: null, localCommitCount: null };
  }
}

/** Freshly verify a ref that contains the exact tip; a stale tracking ref is not authority. */
export async function verifyRemotePreservation(cwd: string, head: string): Promise<string> {
  if (!objectId.test(head)) throw new WorktreeCleanupHold("Checkout identity is unknown.");
  const refs = await preservingRefs(cwd, head);
  const remotesResult = await runGitCommand(["remote"], { cwd });
  const remotes = remotesResult.stdout
    .trim()
    .split("\n")
    .filter(Boolean)
    .sort((a, b) => b.length - a.length);
  // One bounded advertisement read per remote, rather than one network call
  // for every tracking branch that contains HEAD. Offline hosts retain files.
  const deadline = Date.now() + 15_000;
  for (const remote of remotes.slice(0, 4)) {
    const candidates = refs
      .filter(
        (candidate) =>
          remotes.find((name) => candidate.ref.startsWith("refs/remotes/" + name + "/")) === remote,
      )
      .slice(0, 256);
    if (candidates.length === 0 || Date.now() >= deadline) continue;
    const bySource = new Map(
      candidates.map((candidate) => [
        "refs/heads/" + candidate.ref.slice(("refs/remotes/" + remote + "/").length),
        candidate,
      ]),
    );
    try {
      const advertised = await runGitCommand(
        ["ls-remote", "--exit-code", "--refs", remote, ...bySource.keys()],
        {
          cwd,
          timeout: Math.max(1, Math.min(5000, deadline - Date.now())),
          maxOutputBytes: 1024 * 1024,
        },
      );
      if (advertised.truncated) continue;
      let changedTipsChecked = 0;
      for (const line of advertised.stdout.trim().split("\n")) {
        const [sha, ref] = line.split(/\s+/);
        const candidate = ref ? bySource.get(ref) : undefined;
        if (!candidate || !objectId.test(sha ?? "")) continue;
        // This exact cached object was already proved to contain the saved tip.
        if (sha === candidate.head) return candidate.ref;
        if (++changedTipsChecked > 8 || Date.now() >= deadline) break;
        const contained = await runGitCommand(["merge-base", "--is-ancestor", head, sha!], {
          cwd,
          acceptExitCodes: [0, 1, 128],
          timeout: Math.max(1, Math.min(2000, deadline - Date.now())),
        });
        if (contained.exitCode === 0) return candidate.ref;
      }
    } catch {
      // Another configured remote may still provide verifiable preservation.
    }
  }
  throw new WorktreeCleanupHold("The checkout tip is not verified in remote history.");
}

async function ensureLockDirectory(path: string): Promise<void> {
  try {
    await fs.mkdir(path, { mode: 0o700 });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
  }
  const metadata = await fs.lstat(path);
  if (
    !metadata.isDirectory() ||
    metadata.isSymbolicLink() ||
    metadata.uid !== process.getuid?.() ||
    metadata.mode & 0o077
  ) {
    throw new WorktreeCleanupHold("Checkout lock directory ownership is unknown.");
  }
}

/** Use the SAME Linux flock as Muse/Grok/Cursor; no independent lease registry. */
export async function withCheckoutWriteLock<T>(
  gitDir: string,
  operation: (assertHeld: () => Promise<void>) => Promise<T>,
  runtimeDirectory = process.env.XDG_RUNTIME_DIR ?? "/run/user/" + process.getuid?.(),
): Promise<T> {
  if (process.platform !== "linux" || process.getuid === undefined) {
    throw new WorktreeCleanupHold(
      "Automatic checkout cleanup requires a supported ownership check.",
    );
  }
  const runtime = resolve(runtimeDirectory);
  const root = await fs.lstat(runtime);
  if (
    !root.isDirectory() ||
    root.isSymbolicLink() ||
    root.uid !== process.getuid() ||
    root.mode & 0o077
  ) {
    throw new WorktreeCleanupHold("Checkout lock runtime is not private.");
  }
  if ((await fs.realpath(runtime)) !== runtime)
    throw new WorktreeCleanupHold("Checkout lock runtime traverses a link.");
  await ensureLockDirectory(join(runtime, "paseo"));
  const locks = join(runtime, "paseo", "locks");
  await ensureLockDirectory(locks);
  const identity = createHash("sha256").update(gitDir).digest("hex").slice(0, 16);
  const lockPath = join(locks, "grok-" + identity + ".lock");
  const handle = await fs.open(
    lockPath,
    constants.O_CREAT | constants.O_RDWR | constants.O_NOFOLLOW,
    0o600,
  );
  try {
    const initial = await handle.stat();
    if (
      !initial.isFile() ||
      initial.uid !== process.getuid() ||
      initial.nlink !== 1 ||
      initial.mode & 0o077
    ) {
      throw new WorktreeCleanupHold("Checkout lock ownership is unknown.");
    }
    const assertHeld = async () => {
      const current = await fs.lstat(lockPath);
      if (current.dev !== initial.dev || current.ino !== initial.ino || !current.isFile()) {
        throw new WorktreeCleanupHold("Checkout lock changed during cleanup.");
      }
    };
    // flock operates on inherited fd 3. The parent retains the same open file
    // description, so the lock survives helper exit until handle.close().
    await new Promise<void>((resolveLock, rejectLock) => {
      const child = spawn("flock", ["--nonblock", "3"], {
        stdio: ["ignore", "ignore", "ignore", handle.fd],
      });
      child.once("error", () =>
        rejectLock(new WorktreeCleanupHold("Checkout lock could not be acquired.")),
      );
      child.once("close", (code) => {
        if (code === 0) {
          resolveLock();
          return;
        }
        rejectLock(new WorktreeCleanupHold("An implementation worker still owns this checkout."));
      });
    });
    await assertHeld();
    return await operation(assertHeld);
  } finally {
    await handle.close();
  }
}
