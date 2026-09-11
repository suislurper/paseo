import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import { basename, isAbsolute, join, relative, resolve, sep } from "node:path";
import { runGitCommand } from "./run-git-command.js";
import { WorktreeCleanupHold } from "./worktree-preservation.js";

const cacheDirectories = new Set([
  "node_modules",
  ".venv",
  "venv",
  "__pycache__",
  ".pytest_cache",
  ".mypy_cache",
  ".ruff_cache",
  ".import_linter_cache",
  ".next",
  ".turbo",
  "dist",
  "build",
  "coverage",
]);
const protectedDirectories = new Set([
  ".git",
  "data",
  "datasources",
  "state",
  "output",
  "raw",
  "runs",
  "artifacts",
  "evidence",
  "receipts",
  "snapshots",
  "checkpoints",
  "backups",
  "results",
  "logs",
  "models",
  "weights",
  "datasets",
  "corpus",
  "training",
  "experiments",
  "secrets",
  "credentials",
]);
const protectedSuffix =
  /(?:\.sqlite(?:3)?|\.db|\.duckdb|\.mdb|\.wal|\.shm|\.ckpt|\.pt|\.pth|\.safetensors|\.gguf|\.onnx|\.log|\.jsonl|\.done|-(?:wal|shm|journal))$/i;
const maxEntries = 250_000;

function inside(path: string, root: string): boolean {
  const rel = relative(root, path.replace(/ \(deleted\)$/, ""));
  return rel === "" || (!isAbsolute(rel) && rel !== ".." && !rel.startsWith(".." + sep));
}

async function metadataFingerprint(path: string): Promise<string[]> {
  const metadata = await fs.lstat(path, { bigint: true });
  return [
    metadata.dev,
    metadata.ino,
    metadata.mode,
    metadata.size,
    metadata.mtimeNs,
    metadata.ctimeNs,
  ].map(String);
}

function decodeMountPath(value: string): string {
  return value.replace(/\\([0-7]{3})/g, (_, octal: string) =>
    String.fromCharCode(parseInt(octal, 8)),
  );
}

interface CheckoutMount {
  id: string;
  device: string;
  root: string;
  mountpoint: string;
}

function parseCheckoutMount(line: string): CheckoutMount {
  const fields = line.split(" ");
  const root = decodeMountPath(fields[3] ?? "");
  const mountpoint = decodeMountPath(fields[4] ?? "");
  if (
    fields.length < 6 ||
    !/^\d+$/.test(fields[0] ?? "") ||
    !/^\d+:\d+$/.test(fields[2] ?? "") ||
    !isAbsolute(root) ||
    !isAbsolute(mountpoint)
  ) {
    throw new WorktreeCleanupHold("Mount ownership could not be checked.");
  }
  return {
    id: fields[0]!,
    device: fields[2]!,
    root: resolve(root),
    mountpoint: resolve(mountpoint),
  };
}

export async function assertNoCheckoutMounts(worktree: string, procRoot = "/proc"): Promise<void> {
  let text: string;
  try {
    text = await fs.readFile(join(procRoot, "self", "mountinfo"), "utf8");
  } catch {
    throw new WorktreeCleanupHold("Mount ownership could not be checked.");
  }
  const mounts = text.trim().split("\n").map(parseCheckoutMount);
  for (const mount of mounts) {
    if (inside(mount.mountpoint, worktree))
      throw new WorktreeCleanupHold("The checkout contains a mounted path.");
  }
  // mountinfo roots are paths within a filesystem, not necessarily host paths.
  // Resolve the checkout through its deepest containing mount before comparing
  // same-device roots (e.g. /repo in a filesystem mounted at /home/user).
  const containing = mounts
    .filter((mount) => inside(worktree, mount.mountpoint))
    .sort((left, right) => right.mountpoint.length - left.mountpoint.length)[0];
  if (!containing)
    throw new WorktreeCleanupHold("The checkout mount source could not be resolved.");
  const filesystemPath = resolve(containing.root, relative(containing.mountpoint, worktree));
  for (const mount of mounts) {
    if (mount.id === containing.id || mount.device !== containing.device) continue;
    if (inside(mount.root, filesystemPath)) {
      throw new WorktreeCleanupHold("The checkout is a source for another mount.");
    }
    if (inside(filesystemPath, mount.root)) {
      const alias = resolve(mount.mountpoint, relative(mount.root, filesystemPath));
      if (alias !== worktree)
        throw new WorktreeCleanupHold("The checkout is exposed through another mount.");
    }
  }
}

function assertNoMappedCheckoutFiles(maps: string, worktree: string, pid: string): void {
  for (const line of maps.split("\n")) {
    const matched = /^\S+\s+\S+\s+\S+\s+\S+\s+\d+\s+(\/.*)$/.exec(line);
    if (matched && inside(matched[1]!, worktree)) {
      throw new WorktreeCleanupHold("Process " + pid + " still maps a checkout file.");
    }
  }
}

/** Check visible holders, and fail closed for uninspectable coding runtimes. */
export async function assertNoCheckoutProcesses(
  worktree: string,
  procRoot = "/proc",
): Promise<void> {
  if (process.platform !== "linux")
    throw new WorktreeCleanupHold("Process ownership could not be checked.");
  let pids: string[];
  try {
    pids = (await fs.readdir(procRoot)).filter((name) => /^\d+$/.test(name));
  } catch {
    throw new WorktreeCleanupHold("Process ownership could not be checked.");
  }
  let incomplete = false;
  for (const pid of pids) {
    const processPath = join(procRoot, pid);
    let comm: string;
    try {
      if ((await fs.stat(processPath)).uid !== process.getuid?.()) continue;
      comm = (await fs.readFile(join(processPath, "comm"), "utf8")).trim();
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") incomplete = true;
      continue;
    }
    const codingRuntime =
      /^(?:node|python\d*(?:\.\d+)?|git|bash|sh|zsh|fish|omp|grok|codex|claude|bwrap|paseo)(?:$|[-_. ])/i.test(
        comm,
      );
    const inspectLink = async (path: string) => {
      try {
        const target = await fs.readlink(path);
        if (isAbsolute(target) && inside(target, worktree)) {
          throw new WorktreeCleanupHold("Process " + pid + " still uses this checkout.");
        }
      } catch (error) {
        if (error instanceof WorktreeCleanupHold) throw error;
        if ((error as NodeJS.ErrnoException).code !== "ENOENT" && codingRuntime) incomplete = true;
      }
    };
    for (const entry of ["cwd", "root", "exe"]) await inspectLink(join(processPath, entry));
    try {
      for (const fd of await fs.readdir(join(processPath, "fd"))) {
        await inspectLink(join(processPath, "fd", fd));
      }
    } catch (error) {
      if (error instanceof WorktreeCleanupHold) throw error;
      if ((error as NodeJS.ErrnoException).code !== "ENOENT" && codingRuntime) incomplete = true;
    }
    try {
      const maps = await fs.readFile(join(processPath, "maps"), "utf8");
      assertNoMappedCheckoutFiles(maps, worktree, pid);
    } catch (error) {
      if (error instanceof WorktreeCleanupHold) throw error;
      if ((error as NodeJS.ErrnoException).code !== "ENOENT" && codingRuntime) incomplete = true;
    }
  }
  if (incomplete)
    throw new WorktreeCleanupHold("A coding process could not be inspected; files retained.");
}

export interface DisposableCheckoutSnapshot {
  head: string;
  branch: string;
  gitDir: string;
  fingerprint: string;
}

export async function inspectDisposableCheckout(
  worktreePath: string,
  options: { procRoot?: string } = {},
): Promise<DisposableCheckoutSnapshot> {
  const worktree = resolve(worktreePath);
  if ((await fs.realpath(worktree)) !== worktree)
    throw new WorktreeCleanupHold("Checkout path traverses a link.");
  const root = await fs.lstat(worktree);
  if (!root.isDirectory() || root.uid !== process.getuid?.()) {
    throw new WorktreeCleanupHold("Checkout directory ownership is unknown.");
  }
  await assertNoCheckoutMounts(worktree, options.procRoot);
  const git = async (args: string[]) => {
    const value = await runGitCommand(args, { cwd: worktree });
    if (value.truncated) throw new WorktreeCleanupHold("Checkout inspection exceeded its limit.");
    return value.stdout;
  };
  const gitDir = (await git(["rev-parse", "--absolute-git-dir"])).trim();
  const commonDir = resolve(worktree, (await git(["rev-parse", "--git-common-dir"])).trim());
  if (
    resolve(gitDir) === commonDir ||
    (await git(["rev-parse", "--show-toplevel"])).trim() !== worktree
  ) {
    throw new WorktreeCleanupHold("Only an isolated linked checkout can be removed.");
  }
  const head = (await git(["rev-parse", "--verify", "HEAD"])).trim();
  const branch = (await git(["symbolic-ref", "--short", "HEAD"])).trim();
  if (["main", "master", "trunk"].includes(branch))
    throw new WorktreeCleanupHold("Default branches are retained.");
  const remoteHeads = await git(["for-each-ref", "--format=%(symref)", "refs/remotes/"]);
  if (remoteHeads.split("\n").some((ref) => ref.endsWith("/" + branch))) {
    throw new WorktreeCleanupHold("A remote default branch is retained.");
  }
  if (
    (
      await git(["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"])
    ).trim()
  ) {
    throw new WorktreeCleanupHold("The checkout has uncommitted or untracked files.");
  }
  const indexEntries = await git(["ls-files", "-v", "-z"]);
  if (indexEntries.split("\0").some((entry) => entry.startsWith("S ") || /^[a-z] /.test(entry))) {
    throw new WorktreeCleanupHold("The checkout has hidden index changes.");
  }
  // A tracked checkpoint is still data, even if its Git/LFS pointer is preserved.
  for (const entry of indexEntries.split("\0").filter(Boolean)) {
    const tracked = entry.slice(2);
    if (/\.(?:ckpt|pt|pth|safetensors|gguf|onnx|h5|hdf5)$/i.test(tracked)) {
      throw new WorktreeCleanupHold("Files retained: tracked model or data artifact " + tracked);
    }
  }
  const ignored = (
    await git(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"])
  )
    .split("\0")
    .filter(Boolean)
    .map((path) => path.replace(/\/$/, ""));
  const roots = [...new Set(ignored)]
    .sort()
    .filter(
      (path, index, all) => !all.slice(0, index).some((parent) => path.startsWith(parent + "/")),
    );
  const fingerprint: string[][] = [await metadataFingerprint(worktree)];
  let entries = 0;
  const scan = async (path: string, inCache: boolean): Promise<void> => {
    if (++entries > maxEntries)
      throw new WorktreeCleanupHold("Checkout inspection exceeded its entry limit.");
    const rel = relative(worktree, path);
    const name = basename(path);
    if (!inside(path, worktree) || rel === "")
      throw new WorktreeCleanupHold("Ignored path escaped the checkout.");
    if (
      name.startsWith(".env") ||
      protectedDirectories.has(name.toLowerCase()) ||
      protectedSuffix.test(name) ||
      /^(?:checkpoint|model[-_]|weights?[-_]|optimizer|rng_state|training_args|scheduler|trainer_state)/i.test(
        name,
      )
    ) {
      throw new WorktreeCleanupHold("Files retained: protected ignored path " + rel);
    }
    const metadata = await fs.lstat(path);
    if (metadata.uid !== process.getuid?.())
      throw new WorktreeCleanupHold("Ignored path ownership is unknown: " + rel);
    const cache = inCache || cacheDirectories.has(name) || name.endsWith(".egg-info");
    if (!cache) throw new WorktreeCleanupHold("Files retained: unknown ignored path " + rel);
    fingerprint.push([rel, ...(await metadataFingerprint(path))]);
    if (metadata.isSymbolicLink()) return; // Git unlinks cache links; never follow their targets.
    if (metadata.isDirectory()) {
      for (const child of (await fs.readdir(path)).sort()) await scan(join(path, child), cache);
    } else if (!metadata.isFile()) {
      throw new WorktreeCleanupHold("Files retained: special ignored path " + rel);
    }
  };
  for (const rel of roots) await scan(resolve(worktree, rel), false);
  await assertNoCheckoutProcesses(worktree, options.procRoot);
  return {
    head,
    branch,
    gitDir,
    fingerprint: createHash("sha256").update(JSON.stringify(fingerprint)).digest("hex"),
  };
}
