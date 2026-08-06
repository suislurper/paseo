#!/usr/bin/env python3
"""Root-owned read-only process census for Paseo operator cleanup.

Scans /proc without mutating processes. Emits a redacted JSON snapshot of
current processes and only those path references that fall under configured
roots (cwd, exe, interpreter script, open fds). Kernel threads are emitted as
identity-only records so unprivileged consumers can reconcile protected PIDs.

Security contract (see docs/operator-fork.md):
- Install as root-owned, not group/world-writable, typically at
  /usr/local/libexec/paseo-process-census.
- Never run a user-writable copy of this script as root.
- Output never includes argv, command lines, environment, file contents,
  secrets, or paths outside the configured roots.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 1

# Basenames treated as language/shell interpreters when scanning cmdline for a
# script path. Matching is prefix-aware for versioned names (python3.12, nodejs).
_INTERPRETER_PREFIXES = (
    "python",
    "node",
    "nodejs",
    "deno",
    "bun",
    "bash",
    "dash",
    "zsh",
    "sh",
    "perl",
    "ruby",
    "lua",
    "php",
)


class CensusError(Exception):
    """Fatal configuration / output error (not a per-process scan issue)."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def require_absolute(path: str, label: str) -> str:
    if not path or not os.path.isabs(path):
        raise CensusError(f"{label} must be an absolute path: {path!r}")
    return path


def normalize_path(path: str) -> str:
    """Normalize an absolute path for stable comparison and emission."""
    return os.path.normpath(path)


def path_under_any_root(path: str, roots: list[str]) -> bool:
    path_n = normalize_path(path)
    for root in roots:
        root_n = normalize_path(root)
        if path_n == root_n or path_n.startswith(root_n + os.sep):
            return True
    return False


def ensure_no_symlink_components(path: str) -> None:
    """Refuse output paths whose any existing component is a symlink."""
    require_absolute(path, "output")
    # Walk from root downward so intermediate components are checked.
    parts = []
    head = path
    while True:
        parts.append(head)
        parent = os.path.dirname(head)
        if parent == head:
            break
        head = parent
    for component in reversed(parts):
        if os.path.lexists(component) and os.path.islink(component):
            raise CensusError(
                f"refusing output path with symlink component: {component}"
            )


def ensure_parent_dir(path: str, mode: int = 0o755) -> str:
    parent = os.path.dirname(path)
    if not parent:
        raise CensusError(f"output has no parent directory: {path!r}")
    ensure_no_symlink_components(parent if parent != path else path)
    if not os.path.isdir(parent):
        # Create leaf parent only; do not walk creating arbitrary trees with
        # relaxed modes. os.makedirs is fine for /run/paseo style single level.
        os.makedirs(parent, mode=mode, exist_ok=True)
        try:
            os.chmod(parent, mode)
        except OSError:
            pass
    else:
        # Parent exists: still refuse if it is a symlink (already covered) and
        # best-effort enforce expected mode when we own the operator dir.
        try:
            st = os.lstat(parent)
            if stat.S_ISLNK(st.st_mode):
                raise CensusError(f"refusing symlink parent directory: {parent}")
        except FileNotFoundError:
            pass
    # Re-check full output path components after creation.
    ensure_no_symlink_components(path)
    return parent


def atomic_write_json(path: str, payload: dict[str, Any], mode: int = 0o644) -> None:
    """Write JSON atomically in the same directory; fsync file and parent dir."""
    require_absolute(path, "output")
    ensure_no_symlink_components(path)
    parent = ensure_parent_dir(path, mode=0o755)

    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".process-census.", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        tmp_name = ""  # replaced; do not unlink
        # fsync the directory so the rename is durable
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp_name and os.path.lexists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# /proc helpers
# ---------------------------------------------------------------------------


def read_boot_id(proc_root: str) -> str:
    path = os.path.join(proc_root, "sys", "kernel", "random", "boot_id")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        raise CensusError(f"unable to read boot_id: {exc}") from exc


def list_pids(proc_root: str) -> list[int]:
    pids: list[int] = []
    try:
        entries = os.listdir(proc_root)
    except OSError as exc:
        raise CensusError(f"unable to list {proc_root}: {exc}") from exc
    for name in entries:
        if name.isdigit():
            pids.append(int(name))
    pids.sort()
    return pids


def proc_path(proc_root: str, pid: int, *parts: str) -> str:
    return os.path.join(proc_root, str(pid), *parts)


def process_still_exists(proc_root: str, pid: int) -> bool:
    return os.path.isdir(proc_path(proc_root, pid))


def parse_stat_start_and_name(stat_text: str) -> tuple[int, str]:
    """Parse starttime (field 22) and comm from /proc/pid/stat.

    Format: pid (comm) state ppid ... starttime ...
    comm may contain spaces and parentheses; use first '(' and last ')'.
    """
    lparen = stat_text.find("(")
    rparen = stat_text.rfind(")")
    if lparen < 0 or rparen < 0 or rparen <= lparen:
        raise ValueError("malformed stat: missing comm parentheses")
    comm = stat_text[lparen + 1 : rparen]
    rest = stat_text[rparen + 1 :].strip().split()
    # After comm: state(0), ppid(1), ... starttime is index 19 in this rest
    # (field 22 overall: pid=1, comm=2, then fields 3.. → rest index 0 is field 3,
    # so field 22 is rest index 19).
    if len(rest) < 20:
        raise ValueError("malformed stat: too few fields")
    start_time_ticks = int(rest[19])
    return start_time_ticks, comm


def read_stat_identity(
    proc_root: str, pid: int
) -> tuple[int, str] | None:
    """Return (start_time_ticks, name) or None if the process exited."""
    path = proc_path(proc_root, pid, "stat")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    except ProcessLookupError:
        return None
    except OSError:
        # Permission or other read error — caller decides incomplete vs exit.
        raise
    try:
        return parse_stat_start_and_name(text)
    except ValueError:
        raise


def read_status_uid(proc_root: str, pid: int) -> int | None:
    path = proc_path(proc_root, pid, "status")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("Uid:"):
                    parts = line.split()
                    # Uid: real effective saved fs
                    return int(parts[1])
    except FileNotFoundError:
        return None
    except ProcessLookupError:
        return None
    except OSError:
        raise
    return None


def read_cmdline_tokens(proc_root: str, pid: int) -> list[str] | None:
    path = proc_path(proc_root, pid, "cmdline")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except ProcessLookupError:
        return None
    except OSError:
        raise
    if not raw:
        return []
    # cmdline is NUL-separated; trailing NUL is common
    parts = raw.split(b"\0")
    tokens: list[str] = []
    for p in parts:
        if not p:
            continue
        try:
            tokens.append(p.decode("utf-8", errors="surrogateescape"))
        except Exception:
            tokens.append(p.decode("latin-1", errors="replace"))
    return tokens


def readlink_proc(proc_root: str, pid: int, rel: str) -> str | None:
    """Read a /proc pid symlink. None if gone; raise OSError for permission."""
    path = proc_path(proc_root, pid, rel)
    try:
        return os.readlink(path)
    except FileNotFoundError:
        return None
    except ProcessLookupError:
        return None
    except OSError as exc:
        # ENOENT is exit race / kernel thread for exe
        if exc.errno in (errno.ENOENT, errno.ESRCH):
            return None
        raise


def is_kernel_thread(proc_root: str, pid: int) -> bool | None:
    """True if kernel thread, False if userspace, None if process exited.

    Kernel threads have empty cmdline and no resolvable exe (ENOENT).
    """
    try:
        tokens = read_cmdline_tokens(proc_root, pid)
    except OSError:
        # Can't classify; treat as non-kernel and let scan record permission.
        return False
    if tokens is None:
        return None
    if tokens:
        return False
    # Empty cmdline: check exe
    exe_path = proc_path(proc_root, pid, "exe")
    try:
        os.readlink(exe_path)
        # Has an exe link target → userspace (or at least not classic kthread)
        return False
    except FileNotFoundError:
        # No exe entry at all — process gone or kthread-like
        if not process_still_exists(proc_root, pid):
            return None
        return True
    except ProcessLookupError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ESRCH):
            if not process_still_exists(proc_root, pid):
                return None
            return True
        if exc.errno in (errno.EACCES, errno.EPERM):
            # Can't tell; not safe to drop as kernel
            return False
        if not process_still_exists(proc_root, pid):
            return None
        return False


def basename_is_interpreter(name: str) -> bool:
    base = os.path.basename(name).lower()
    if not base:
        return False
    for prefix in _INTERPRETER_PREFIXES:
        if base == prefix:
            return True
        if base.startswith(prefix):
            rest = base[len(prefix) :]
            if not rest or rest[0] in ".-0123456789":
                return True
    return False


def extract_interpreter_script(
    tokens: list[str], cwd: str | None, roots: list[str]
) -> str | None:
    """If argv looks like an interpreter invocation, return the script path under roots."""
    if len(tokens) < 2:
        return None
    if not basename_is_interpreter(tokens[0]):
        return None
    i = 1
    while i < len(tokens):
        arg = tokens[i]
        if arg == "--":
            i += 1
            break
        if arg.startswith("-"):
            # Options that take a following argument (best-effort, not exhaustive).
            # We only care about finding a real script path; skip known value opts.
            opt = arg.split("=", 1)[0]
            if opt in {
                "-c",
                "-e",
                "-m",  # python -m module (not a script file path we report)
                "--eval",
                "--print",
                "-W",
                "-X",
            } and "=" not in arg and i + 1 < len(tokens):
                i += 2
                continue
            i += 1
            continue
        break
    if i >= len(tokens):
        return None
    candidate = tokens[i]
    # python -m module name is not a path; skip non-path-looking tokens
    if candidate.startswith("-"):
        return None
    if not os.path.isabs(candidate):
        if not cwd:
            return None
        candidate = os.path.join(cwd, candidate)
    candidate = normalize_path(candidate)
    if path_under_any_root(candidate, roots):
        return candidate
    return None


def is_filesystem_path_target(target: str) -> bool:
    """True if a /proc fd/cwd/exe readlink target looks like a real path."""
    if not target:
        return False
    if target.startswith(("socket:", "pipe:", "anon_inode:", "net:")):
        return False
    # Deleted files: "/path (deleted)" — strip suffix for root check
    if target.endswith(" (deleted)"):
        target = target[: -len(" (deleted)")]
    return target.startswith("/")


def clean_link_target(target: str) -> str:
    if target.endswith(" (deleted)"):
        target = target[: -len(" (deleted)")]
    return normalize_path(target)


def collect_open_fd_refs(
    proc_root: str, pid: int, roots: list[str]
) -> tuple[list[dict[str, str]], bool]:
    """Return (references, scope_ok). scope_ok False on permission while alive."""
    fd_dir = proc_path(proc_root, pid, "fd")
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        names = os.listdir(fd_dir)
    except FileNotFoundError:
        return [], True  # process exited mid-scan
    except ProcessLookupError:
        return [], True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            if process_still_exists(proc_root, pid):
                return [], False
            return [], True
        if not process_still_exists(proc_root, pid):
            return [], True
        return [], False

    for name in names:
        link = proc_path(proc_root, pid, "fd", name)
        try:
            target = os.readlink(link)
        except FileNotFoundError:
            continue
        except ProcessLookupError:
            continue
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                if process_still_exists(proc_root, pid):
                    return refs, False
                return refs, True
            continue
        if not is_filesystem_path_target(target):
            continue
        cleaned = clean_link_target(target)
        if not path_under_any_root(cleaned, roots):
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        refs.append({"kind": "open_fd", "path": cleaned})
    return refs, True


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def error_class_for_oserror(exc: OSError) -> str:
    if exc.errno in (errno.EACCES, errno.EPERM):
        return "permission"
    return "read_error"


def scan_process(
    proc_root: str, pid: int, roots: list[str]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
    """Scan one pid.

    Returns (record_or_None, error_or_None, incomplete).
    None record means the process exited (skip silently).
    incomplete True means still-existing process had a read/permission failure.
    """
    kthread = is_kernel_thread(proc_root, pid)
    if kthread is None:
        return None, None, False
    if kthread:
        try:
            identity = read_stat_identity(proc_root, pid)
        except (OSError, ValueError) as exc:
            if not process_still_exists(proc_root, pid):
                return None, None, False
            error_class = (
                error_class_for_oserror(exc)
                if isinstance(exc, OSError)
                else "read_error"
            )
            return (
                {"pid": pid, "start_time_ticks": None, "error": error_class},
                {"pid": pid, "start_time_ticks": None, "class": error_class},
                True,
            )
        if identity is None:
            return None, None, False
        start_time_ticks, name = identity
        return (
            {
                "pid": pid,
                "start_time_ticks": start_time_ticks,
                "uid": 0,
                "name": name,
                "scope_complete": True,
                "references": [],
                "kernel_thread": True,
            },
            None,
            False,
        )

    start_time_ticks: int | None = None
    name: str | None = None
    try:
        identity = read_stat_identity(proc_root, pid)
    except OSError as exc:
        if not process_still_exists(proc_root, pid):
            return None, None, False
        # Without start time we still record what we can for blocking consumers.
        err = {
            "pid": pid,
            "start_time_ticks": None,
            "class": error_class_for_oserror(exc),
        }
        rec = {
            "pid": pid,
            "start_time_ticks": None,
            "error": error_class_for_oserror(exc),
        }
        return rec, err, True
    except ValueError:
        if not process_still_exists(proc_root, pid):
            return None, None, False
        err = {"pid": pid, "start_time_ticks": None, "class": "read_error"}
        rec = {"pid": pid, "start_time_ticks": None, "error": "read_error"}
        return rec, err, True

    if identity is None:
        return None, None, False
    start_time_ticks, name = identity

    try:
        uid = read_status_uid(proc_root, pid)
    except OSError as exc:
        if not process_still_exists(proc_root, pid):
            return None, None, False
        err = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "class": error_class_for_oserror(exc),
        }
        rec = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "error": error_class_for_oserror(exc),
        }
        return rec, err, True

    if uid is None:
        if not process_still_exists(proc_root, pid):
            return None, None, False
        # status vanished mid-read but dir might race; treat as exit if gone
        err = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "class": "read_error",
        }
        rec = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "error": "read_error",
        }
        return rec, err, True

    references: list[dict[str, str]] = []
    scope_complete = True

    # cwd
    try:
        cwd_target = readlink_proc(proc_root, pid, "cwd")
    except OSError as exc:
        if not process_still_exists(proc_root, pid):
            return None, None, False
        err = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "class": error_class_for_oserror(exc),
        }
        rec = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "error": error_class_for_oserror(exc),
        }
        return rec, err, True

    cwd_path: str | None = None
    if cwd_target is not None and is_filesystem_path_target(cwd_target):
        cwd_path = clean_link_target(cwd_target)
        if path_under_any_root(cwd_path, roots):
            references.append({"kind": "cwd", "path": cwd_path})

    # exe
    try:
        exe_target = readlink_proc(proc_root, pid, "exe")
    except OSError as exc:
        if not process_still_exists(proc_root, pid):
            return None, None, False
        err = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "class": error_class_for_oserror(exc),
        }
        rec = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "error": error_class_for_oserror(exc),
        }
        return rec, err, True

    if exe_target is not None and is_filesystem_path_target(exe_target):
        exe_path = clean_link_target(exe_target)
        if path_under_any_root(exe_path, roots):
            references.append({"kind": "exe", "path": exe_path})

    # cmdline — internal only for interpreter script detection
    try:
        tokens = read_cmdline_tokens(proc_root, pid)
    except OSError as exc:
        if not process_still_exists(proc_root, pid):
            return None, None, False
        err = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "class": error_class_for_oserror(exc),
        }
        rec = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "error": error_class_for_oserror(exc),
        }
        return rec, err, True

    if tokens is None:
        if not process_still_exists(proc_root, pid):
            return None, None, False
        # Unexpected: alive but no cmdline file
        scope_complete = False
        tokens = []

    script = extract_interpreter_script(tokens, cwd_path, roots)
    if script is not None:
        references.append({"kind": "interpreter_script", "path": script})

    # open fds
    fd_refs, fd_ok = collect_open_fd_refs(proc_root, pid, roots)
    if not fd_ok:
        # Permission while still alive — fail closed for this process identity
        err = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "class": "permission",
        }
        rec = {
            "pid": pid,
            "start_time_ticks": start_time_ticks,
            "error": "permission",
        }
        return rec, err, True
    references.extend(fd_refs)

    # Stable order: kind then path
    kind_order = {"cwd": 0, "exe": 1, "interpreter_script": 2, "open_fd": 3}
    references.sort(key=lambda r: (kind_order.get(r["kind"], 9), r["path"]))

    record = {
        "pid": pid,
        "start_time_ticks": start_time_ticks,
        "uid": uid,
        "name": name,
        "scope_complete": scope_complete,
        "references": references,
    }
    return record, None, False


def build_snapshot(
    *,
    proc_root: str,
    roots: list[str],
    captured_at: str | None = None,
) -> dict[str, Any]:
    roots_norm = [normalize_path(require_absolute(r, "root")) for r in roots]
    # Deduplicate while preserving order
    seen_roots: set[str] = set()
    roots_unique: list[str] = []
    for r in roots_norm:
        if r not in seen_roots:
            seen_roots.add(r)
            roots_unique.append(r)

    boot_id = read_boot_id(proc_root)
    if captured_at is None:
        captured_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    processes: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    complete = True

    for pid in list_pids(proc_root):
        record, err, incomplete = scan_process(proc_root, pid, roots_unique)
        if incomplete:
            complete = False
        if err is not None:
            errors.append(err)
        if record is not None:
            processes.append(record)

    return {
        "schema_version": SCHEMA_VERSION,
        "boot_id": boot_id,
        "captured_at": captured_at,
        "roots": roots_unique,
        "complete": complete,
        "errors": errors,
        "processes": processes,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only process census for Paseo operator cleanup. "
            "Scans /proc; emits redacted JSON under configured roots only."
        )
    )
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        default=[],
        metavar="PATH",
        help="Absolute path root; only references under these roots are emitted. "
        "Repeatable. Production: /home/user/.paseo/worktrees and "
        "/mnt/data/paseo-runtime.",
    )
    parser.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="Absolute output JSON path (e.g. /run/paseo/process-census.json).",
    )
    parser.add_argument(
        "--proc-root",
        default="/proc",
        help=argparse.SUPPRESS,  # test/injection only
    )
    parser.add_argument(
        "--print",
        action="store_true",
        dest="print_stdout",
        help="Also print the snapshot JSON to stdout.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if not args.roots:
            raise CensusError("at least one --root is required")
        for r in args.roots:
            require_absolute(r, "root")
        require_absolute(args.output, "output")
        ensure_no_symlink_components(args.output)

        snapshot = build_snapshot(proc_root=args.proc_root, roots=args.roots)
        atomic_write_json(args.output, snapshot, mode=0o644)
        if args.print_stdout:
            json.dump(snapshot, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        return 0
    except CensusError as exc:
        print(f"process-census: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"process-census: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
