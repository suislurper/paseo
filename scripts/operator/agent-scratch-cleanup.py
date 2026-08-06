#!/usr/bin/env python3
"""Fail-closed managed agent-scratch probe + exact UUID archive (tombstone)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

SCHEMA_VERSION = 1
MANIFEST_SCHEMA = 1
LOCK_OWNER_SCHEMA = 1
RELEASE_GRACE_S = 24 * 3600
SIZE_WALK_TIMEOUT_S = 60
INVENTORY_TIMEOUT_S = 60
SNAPSHOT_MAX_AGE_S = 45
# Total bounded window for wait-for-acceptable-snapshot + process_proof (+ transient retries).
SNAPSHOT_WAIT_S = 75
# Process-proof failures that may be pure timer/scan races; only these may re-wait.
TRANSIENT_PROCESS_REASONS = frozenset(
    {"process_new", "process_pid_mismatch", "live_process_race"}
)
# Plain JSON list length at/above this matches the worktree probe page-cap ambiguity.
CLI_PAGE_CAP = 200
DEFAULT_CENSUS = "/run/paseo/process-census.json"
LOCK_OP = "archive_scratch"
MANIFEST_NAME = "manifest.json"
OWNER_NAME = "owner.json"
TOMBSTONE_REL = os.path.join("quarantine", "released-scratch")
REF_KINDS = frozenset({"cwd", "exe", "interpreter_script", "open_fd"})
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


class ToolError(Exception):
    """Fatal probe/archive error."""


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_uuid(value: str) -> bool:
    return bool(UUID_RE.match(value or ""))


def require_abs(path: str, label: str) -> str:
    if not path or not os.path.isabs(path):
        raise ToolError(f"{label} must be an absolute path: {path!r}")
    return path


def norm(path: str) -> str:
    return os.path.normpath(path)


def under(path: str, root: str) -> bool:
    p, r = norm(path), norm(root)
    # os.path.normpath("/") + sep becomes "//"; treat filesystem root specially.
    if r == os.sep:
        return p == os.sep or p.startswith(os.sep)
    return p == r or p.startswith(r + os.sep)


def free_bytes(path: str) -> int:
    cur = path
    while True:
        try:
            st = os.statvfs(cur)
            return int(st.f_bavail) * int(st.f_frsize)
        except FileNotFoundError:
            parent = os.path.dirname(cur)
            if parent == cur:
                raise ToolError(f"cannot statvfs free space for {path}")
            cur = parent


def ensure_no_symlink_components(path: str, label: str = "path") -> None:
    """Refuse if any existing component of path is a symlink (fail closed)."""
    require_abs(path, label)
    head = norm(path)
    parts: list[str] = []
    while True:
        parts.append(head)
        parent = os.path.dirname(head)
        if parent == head:
            break
        head = parent
    for component in reversed(parts):
        if os.path.lexists(component) and os.path.islink(component):
            raise ToolError(f"{label} has symlink component: {component}")


def fsync_dir(path: str) -> None:
    ensure_no_symlink_components(path, "directory")
    dir_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def mkdir_exact(path: str, mode: int = 0o700) -> None:
    """Create a single directory with exact mode; fail closed on chmod."""
    ensure_no_symlink_components(path, "directory")
    parent = os.path.dirname(path)
    if parent and parent != path:
        ensure_no_symlink_components(parent, "parent directory")
        if not os.path.isdir(parent) or os.path.islink(parent):
            raise ToolError(f"parent directory missing or not a real dir: {parent}")
    if os.path.lexists(path):
        if os.path.islink(path):
            raise ToolError(f"path is a symlink: {path}")
        if not os.path.isdir(path):
            raise ToolError(f"path exists and is not a directory: {path}")
        try:
            os.chmod(path, mode)
        except OSError as exc:
            raise ToolError(f"chmod failed for existing directory {path}: {exc}") from exc
        return
    try:
        os.mkdir(path, mode)
    except FileExistsError as exc:
        raise ToolError(f"directory race creating {path}") from exc
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise ToolError(f"chmod failed after mkdir {path}: {exc}") from exc


def atomic_write(path: str, data: str, mode: int = 0o600) -> None:
    """Atomic write that refuses symlink parents and fsyncs file + directory."""
    require_abs(path, "atomic write path")
    ensure_no_symlink_components(path, "atomic write path")
    parent = os.path.dirname(path)
    if not parent:
        raise ToolError(f"atomic write has no parent: {path}")
    ensure_no_symlink_components(parent, "atomic write parent")
    if not os.path.isdir(parent) or os.path.islink(parent):
        raise ToolError(f"atomic write parent is not a real directory: {parent}")
    tmp = os.path.join(parent, f".tmp.{os.getpid()}.{time.time_ns()}")
    ensure_no_symlink_components(tmp, "atomic write tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError as exc:
            raise ToolError(f"chmod failed for temp file {tmp}: {exc}") from exc
        os.replace(tmp, path)
        tmp = ""
        fsync_dir(parent)
    finally:
        if tmp and os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Config / aggregate size
# ---------------------------------------------------------------------------


def load_runtime_root(config_path: str) -> str:
    require_abs(config_path, "config")
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"cannot read config {config_path}: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ToolError("config root must be an object")
    agents = cfg.get("agents")
    if not isinstance(agents, dict):
        raise ToolError("config.agents.runtimeRoot is required")
    root = agents.get("runtimeRoot")
    if not isinstance(root, str) or not root.strip():
        raise ToolError("config.agents.runtimeRoot is required")
    root = norm(root.strip())
    require_abs(root, "agents.runtimeRoot")
    ensure_no_symlink_components(root, "runtimeRoot")
    if not os.path.isdir(root):
        raise ToolError(f"runtimeRoot is not a directory: {root}")
    return root


def dir_size_capped(path: str, deadline: float) -> tuple[int | None, str | None]:
    """Non-following size walk. Returns (bytes, error_reason). Never returns 0 on error."""
    if not os.path.lexists(path):
        return 0, None
    total = 0
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            return None, "size_walk_timeout"
        cur = stack.pop()
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            continue
        except OSError:
            return None, "size_walk_unreadable"
        if stat.S_ISLNK(st.st_mode) or stat.S_ISREG(st.st_mode):
            total += st.st_size
            continue
        if not stat.S_ISDIR(st.st_mode):
            total += st.st_size
            continue
        try:
            with os.scandir(cur) as it:
                for ent in it:
                    if time.monotonic() > deadline:
                        return None, "size_walk_timeout"
                    stack.append(ent.path)
        except FileNotFoundError:
            continue
        except OSError:
            return None, "size_walk_unreadable"
    return total, None


def report_aggregate(runtime_root: str, name: str, deadline: float) -> dict[str, Any]:
    """Report-only size. Timeouts/unreadable → unknown + error, never 0."""
    path = os.path.join(runtime_root, name)
    size, err = dir_size_capped(path, deadline)
    if err is not None:
        return {"bytes": None, "status": "unknown", "error": err}
    return {"bytes": int(size or 0), "status": "ok", "error": None}


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def read_json_file(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def classify_manifest(
    scratch_dir: str, agent_id: str, now: datetime
) -> tuple[str, list[str], dict[str, Any] | None]:
    reasons: list[str] = []
    if os.path.islink(scratch_dir):
        return "blocked", ["scratch_is_symlink"], None
    if not os.path.isdir(scratch_dir):
        return "blocked", ["scratch_not_directory"], None
    man_path = os.path.join(scratch_dir, MANIFEST_NAME)
    if not os.path.lexists(man_path):
        return "blocked", ["manifest_missing"], None
    if os.path.islink(man_path):
        return "blocked", ["manifest_is_symlink"], None
    try:
        raw = read_json_file(man_path)
    except (OSError, json.JSONDecodeError):
        return "blocked", ["manifest_unparseable"], None
    if not isinstance(raw, dict):
        return "blocked", ["manifest_malformed"], None
    if raw.get("schemaVersion") != MANIFEST_SCHEMA:
        return "blocked", ["manifest_schema"], None
    if raw.get("agentId") != agent_id or not is_uuid(str(raw.get("agentId", ""))):
        return "blocked", ["manifest_agent_id_mismatch"], None
    gen = raw.get("generation")
    if not isinstance(gen, str) or not is_uuid(gen):
        return "blocked", ["manifest_generation_invalid"], None
    life = raw.get("lifecycle")
    if life == "active":
        return "protected", ["lifecycle_active"], raw
    if life != "released":
        return "blocked", ["lifecycle_unknown"], raw
    released_at = raw.get("releasedAt")
    if not isinstance(released_at, str) or not released_at.strip():
        return "blocked", ["released_at_missing"], raw
    try:
        rel_dt = parse_iso(released_at)
    except (TypeError, ValueError):
        return "blocked", ["released_at_invalid"], raw
    if (now - rel_dt).total_seconds() < RELEASE_GRACE_S:
        return "protected", ["release_grace"], raw
    return "ok", [], raw


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


def lock_dir_for(runtime_root: str, agent_id: str) -> str:
    return os.path.join(runtime_root, "locks", f"{agent_id}.lock")


def lock_present(runtime_root: str, agent_id: str) -> tuple[bool, str | None]:
    ld = lock_dir_for(runtime_root, agent_id)
    if not os.path.lexists(ld):
        return False, None
    if os.path.islink(ld):
        return True, "lock_is_symlink"
    return True, "lock_present"


def ensure_locks_parent(runtime_root: str) -> str:
    """Exact locks parent under runtimeRoot. Fail closed on symlink / chmod."""
    ensure_no_symlink_components(runtime_root, "runtimeRoot")
    locks_parent = os.path.join(runtime_root, "locks")
    ensure_no_symlink_components(locks_parent, "locks parent")
    mkdir_exact(locks_parent, 0o700)
    return locks_parent


def acquire_lock(runtime_root: str, agent_id: str) -> tuple[str, str]:
    """mkdir lock + owner.json. Never breaks by age."""
    ensure_locks_parent(runtime_root)
    ld = lock_dir_for(runtime_root, agent_id)
    ensure_no_symlink_components(ld, "lock path")
    if os.path.lexists(ld):
        if os.path.islink(ld):
            raise ToolError(f"agent lock path is a symlink: {ld}")
        raise ToolError(
            f"agent lock already held or present for {agent_id} "
            "(stale locks require operator inspection)"
        )
    try:
        os.mkdir(ld, 0o700)
    except FileExistsError as exc:
        raise ToolError(
            f"agent lock already held for {agent_id} (stale locks require operator inspection)"
        ) from exc
    try:
        os.chmod(ld, 0o700)
    except OSError as exc:
        raise ToolError(f"chmod failed on lock directory {ld}: {exc}") from exc
    token = str(uuid.uuid4())
    boot_id = None
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as f:
            boot_id = f.read().strip() or None
    except OSError:
        boot_id = None
    owner: dict[str, Any] = {
        "schemaVersion": LOCK_OWNER_SCHEMA,
        "agentId": agent_id,
        "lockToken": token,
        "operation": LOCK_OP,
        "pid": os.getpid(),
        "acquiredAt": utc_now_iso(),
    }
    if boot_id:
        owner["bootId"] = boot_id
    atomic_write(os.path.join(ld, OWNER_NAME), json.dumps(owner, indent=2, sort_keys=True) + "\n")
    return ld, token


def release_lock(lock_dir: str, agent_id: str, lock_token: str) -> None:
    ensure_no_symlink_components(lock_dir, "lock path")
    if os.path.islink(lock_dir):
        raise ToolError(f"cannot release agent lock: lock path is a symlink: {lock_dir}")
    if not os.path.isdir(lock_dir):
        raise ToolError(f"cannot release agent lock: lock path is not a directory: {lock_dir}")
    owner_path = os.path.join(lock_dir, OWNER_NAME)
    try:
        owner = read_json_file(owner_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(
            f"cannot release agent lock: owner.json missing or unreadable for {agent_id}"
        ) from exc
    if not isinstance(owner, dict):
        raise ToolError(f"cannot release agent lock: invalid owner.json for {agent_id}")
    if owner.get("agentId") != agent_id or owner.get("lockToken") != lock_token:
        raise ToolError(f"cannot release agent lock: token mismatch for {agent_id}")
    try:
        entries = os.listdir(lock_dir)
    except OSError as exc:
        raise ToolError(
            f"cannot release agent lock: failed to list lock directory for {agent_id}"
        ) from exc
    if set(entries) != {OWNER_NAME}:
        raise ToolError(
            f"cannot release agent lock: unexpected contents in lock directory for {agent_id}"
        )
    try:
        os.unlink(owner_path)
    except OSError as exc:
        raise ToolError(
            f"cannot release agent lock: failed to remove owner.json for {agent_id}"
        ) from exc
    try:
        os.rmdir(lock_dir)
    except OSError as exc:
        raise ToolError(
            f"cannot release agent lock: failed to remove lock directory for {agent_id}"
        ) from exc


# ---------------------------------------------------------------------------
# Paseo CLI census
# ---------------------------------------------------------------------------


def default_paseo_runner(paseo_bin: str) -> Callable[[list[str]], tuple[int, str, str]]:
    def run(args: list[str]) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                [paseo_bin, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", str(exc)
        return proc.returncode, proc.stdout, proc.stderr

    return run


def parse_cli_json(stdout: str, label: str) -> Any:
    text = stdout.strip()
    if not text:
        raise ToolError(f"{label}: empty output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError(f"{label}: unparseable json: {exc}") from exc
    if isinstance(data, dict):
        if data.get("page_capped") is True or data.get("truncated") is True:
            raise ToolError(f"{label}: page_capped")
        page = data.get("pageInfo") or data.get("page_info")
        if isinstance(page, dict) and page.get("hasMore") is True:
            raise ToolError(f"{label}: page_capped")
        if data.get("error"):
            raise ToolError(f"{label}: {data.get('error')}")
    if isinstance(data, list) and len(data) >= CLI_PAGE_CAP:
        # Canonical worktree probe: plain list at the CLI page size is ambiguous.
        raise ToolError(f"{label}: page_capped")
    return data


def cli_json(runner: Callable[[list[str]], tuple[int, str, str]], args: list[str], label: str) -> Any:
    code, out, err = runner([*args, "--json"] if "--json" not in args else args)
    if code != 0:
        raise ToolError(f"{label}: exit {code}: {err or out}")
    return parse_cli_json(out, label)


def _require_agent_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{label}: missing agent id")
    aid = value.strip()
    if not is_uuid(aid):
        raise ToolError(f"{label}: non-uuid or ambiguous agent id {aid!r}")
    return aid


def inspect_agent(
    runner: Callable[[list[str]], tuple[int, str, str]], agent_id: str
) -> dict[str, Any]:
    data = cli_json(runner, ["inspect", agent_id], f"paseo inspect {agent_id}")
    if not isinstance(data, dict):
        raise ToolError(f"paseo inspect {agent_id}: expected object")
    return data


def inspect_schedule_identity(
    runner: Callable[[list[str]], tuple[int, str, str]], schedule_id: str
) -> dict[str, Any]:
    data = cli_json(
        runner,
        ["schedule", "inspect", schedule_id, "--identity-only"],
        f"paseo schedule inspect {schedule_id} --identity-only",
    )
    if not isinstance(data, dict):
        raise ToolError(f"schedule identity {schedule_id}: expected object")
    return data


def interpret_schedule_identity_target(
    identity: dict[str, Any],
    *,
    schedule_id: str,
) -> tuple[str, str]:
    """Interpret an authoritative identity-only schedule target.

    Bounded identity omits configured cwd/workspace for new-agent targets, so those
    (and unknown/malformed targets) must globally block every cleanup candidate.

    Returns:
      ("agent", agent_id) for exact agent/self targets.
      ("global_block", reason) for new-agent or unknown/malformed targets.
        new-agent uses reason ``active_new_agent_schedule``.
    """
    target = identity.get("target")
    if not isinstance(target, dict):
        return "global_block", f"schedule_identity_malformed:{schedule_id}"
    ttype = target.get("type")
    if ttype == "new-agent":
        return "global_block", "active_new_agent_schedule"
    if ttype not in ("agent", "self"):
        return "global_block", f"schedule_identity_unknown_target:{schedule_id}"
    tid = target.get("agentId") or target.get("agent_id")
    try:
        return "agent", _require_agent_id(tid, f"schedule identity {schedule_id}")
    except ToolError:
        return "global_block", f"schedule_identity_malformed:{schedule_id}"


def collect_paseo_census(
    runner: Callable[[list[str]], tuple[int, str, str]],
) -> dict[str, Any]:
    """Read-only census. Fail closed on malformed/ambiguous/page-capped entries."""
    unarchived = cli_json(runner, ["ls", "-g"], "paseo ls -g")
    if not isinstance(unarchived, list):
        raise ToolError("paseo ls -g: expected JSON array")
    unarchived_ids: set[str] = set()
    for item in unarchived:
        if not isinstance(item, dict):
            raise ToolError("paseo ls -g: malformed agent entry")
        aid = item.get("id") if isinstance(item.get("id"), str) else item.get("Id")
        unarchived_ids.add(_require_agent_id(aid, "paseo ls -g"))

    schedules = cli_json(runner, ["schedule", "ls"], "paseo schedule ls")
    if not isinstance(schedules, list):
        raise ToolError("paseo schedule ls: expected JSON array")
    for sch in schedules:
        if not isinstance(sch, dict):
            raise ToolError("paseo schedule ls: malformed entry")
        sid = sch.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise ToolError("paseo schedule ls: missing schedule id")
        status = sch.get("status")
        if not isinstance(status, str) or not status:
            raise ToolError("paseo schedule ls: missing schedule status")

    permits = cli_json(runner, ["permit", "ls"], "paseo permit ls")
    if not isinstance(permits, list):
        raise ToolError("paseo permit ls: expected JSON array")
    for perm in permits:
        if not isinstance(perm, dict):
            raise ToolError("paseo permit ls: malformed entry")
        aid = perm.get("agentId") or perm.get("agent_id")
        _require_agent_id(aid, "paseo permit ls")

    terminals = cli_json(runner, ["terminal", "ls", "--all"], "paseo terminal ls --all")
    if not isinstance(terminals, list):
        raise ToolError("paseo terminal ls: expected JSON array")
    for term in terminals:
        if not isinstance(term, dict):
            raise ToolError("paseo terminal ls: malformed entry")
        cwd = term.get("cwd")
        if cwd is None or cwd == "" or cwd == "-":
            raise ToolError("paseo terminal ls: ambiguous terminal cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            raise ToolError("paseo terminal ls: non-absolute terminal cwd")

    # Authoritative active-schedule targets via identity-only inspect.
    protected_schedule_agents: set[str] = set()
    global_schedule_blocks: list[str] = []
    for sch in schedules:
        if sch.get("status") != "active":
            continue
        sid = str(sch["id"])
        identity = inspect_schedule_identity(runner, sid)
        kind, payload = interpret_schedule_identity_target(identity, schedule_id=sid)
        if kind == "global_block":
            if payload not in global_schedule_blocks:
                global_schedule_blocks.append(payload)
            continue
        protected_schedule_agents.add(payload)

    # Descendant ancestry via authoritative inspect ParentAgentId (not --label).
    parent_of: dict[str, str | None] = {}
    for aid in sorted(unarchived_ids):
        try:
            info = inspect_agent(runner, aid)
        except ToolError as exc:
            raise ToolError(f"descendant ancestry inspect failed for {aid}: {exc}") from exc
        parent = info.get("ParentAgentId")
        if parent is None or parent == "null" or parent == "":
            parent_of[aid] = None
            continue
        if not isinstance(parent, str):
            raise ToolError(f"descendant ancestry: malformed ParentAgentId for {aid}")
        parent = parent.strip()
        if not is_uuid(parent):
            raise ToolError(f"descendant ancestry: non-uuid ParentAgentId for {aid}")
        parent_of[aid] = parent

    validate_unarchived_ancestry(parent_of, unarchived_ids)

    return {
        "unarchived_ids": unarchived_ids,
        "unarchived": unarchived,
        "schedules": schedules,
        "permits": permits,
        "terminals": terminals,
        "protected_schedule_agents": protected_schedule_agents,
        "global_schedule_blocks": global_schedule_blocks,
        "parent_of": parent_of,
    }


def validate_unarchived_ancestry(
    parent_of: dict[str, str | None], unarchived_ids: set[str]
) -> None:
    """Fail closed on cycles or unresolved unarchived ParentAgentId nodes."""
    for aid in sorted(unarchived_ids):
        if aid not in parent_of:
            raise ToolError(f"descendant ancestry: unresolved node {aid}")
        seen: set[str] = set()
        cur: str | None = aid
        while cur is not None:
            if cur in seen:
                raise ToolError(f"descendant ancestry: cycle involving {aid}")
            seen.add(cur)
            if cur not in parent_of:
                # Left the inspected unarchived set only via a non-unarchived parent.
                if cur in unarchived_ids:
                    raise ToolError(f"descendant ancestry: unresolved node {cur}")
                break
            parent = parent_of[cur]
            if parent is None:
                break
            if parent in unarchived_ids and parent not in parent_of:
                raise ToolError(f"descendant ancestry: unresolved node {parent}")
            if parent not in parent_of and parent not in unarchived_ids:
                # Archived/external parent terminates the unarchived subgraph.
                break
            cur = parent


def agent_is_archived_or_closed(info: dict[str, Any]) -> tuple[bool, list[str]]:
    archived_at = info.get("ArchivedAt")
    if archived_at is None and "archivedAt" in info:
        archived_at = info.get("archivedAt")
    status = info.get("Status") or info.get("status")
    if isinstance(archived_at, str) and archived_at.strip() and archived_at != "null":
        return True, []
    if status == "closed":
        return True, []
    if archived_at is True or info.get("Archived") is True:
        return True, []
    return False, ["agent_not_archived_or_closed"]


def has_unarchived_descendant(agent_id: str, parent_of: dict[str, str | None], unarchived_ids: set[str]) -> bool:
    """True if any unarchived agent has agent_id in its parent chain.

    Cycles and unresolved unarchived nodes are rejected at census time; this walk
    still fails closed if it encounters them rather than assuming safe.
    """
    for aid in unarchived_ids:
        if aid == agent_id:
            continue
        seen: set[str] = set()
        cur: str | None = aid
        while cur is not None:
            if cur in seen:
                raise ToolError(f"descendant ancestry: cycle involving {aid}")
            seen.add(cur)
            if cur not in parent_of:
                if cur in unarchived_ids:
                    raise ToolError(f"descendant ancestry: unresolved node {cur}")
                break
            parent = parent_of[cur]
            if parent == agent_id:
                return True
            if parent is None:
                break
            if parent in unarchived_ids and parent not in parent_of:
                raise ToolError(f"descendant ancestry: unresolved node {parent}")
            if parent not in parent_of and parent not in unarchived_ids:
                break
            cur = parent
    return False


def permit_for_agent(permit: dict[str, Any], agent_id: str) -> bool:
    aid = permit.get("agentId") or permit.get("agent_id")
    return isinstance(aid, str) and aid == agent_id


def terminal_in_scratch(terminal: dict[str, Any], scratch_dir: str) -> bool:
    cwd = terminal.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        # Ambiguity is rejected at census collection; this is a last-resort block.
        raise ToolError("terminal_cwd_ambiguous")
    return under(norm(cwd), scratch_dir)


# ---------------------------------------------------------------------------
# Process census merge
# ---------------------------------------------------------------------------


def read_boot_id(proc_root: str) -> str:
    path = os.path.join(proc_root, "sys", "kernel", "random", "boot_id")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        raise ToolError(f"unable to read boot_id: {exc}") from exc


PF_KTHREAD = 0x00200000


def parse_stat_identity(stat_text: str) -> tuple[int, bool]:
    """Return start ticks and kernel-thread identity from one proc stat read."""
    lparen = stat_text.find("(")
    rparen = stat_text.rfind(")")
    if lparen < 0 or rparen < 0 or rparen <= lparen:
        raise ValueError("malformed stat")
    rest = stat_text[rparen + 2 :].split()
    if len(rest) < 20:
        raise ValueError("malformed stat")
    return int(rest[19]), bool(int(rest[6]) & PF_KTHREAD)


def parse_stat_start(stat_text: str) -> int:
    return parse_stat_identity(stat_text)[0]


def _proc_path(proc_root: str, pid: int, *parts: str) -> str:
    return os.path.join(proc_root, str(pid), *parts)


def _process_still_exists(proc_root: str, pid: int) -> bool:
    return os.path.isdir(_proc_path(proc_root, pid))


def live_pid_map(
    proc_root: str,
    *,
    deadline_mono: float | None = None,
) -> tuple[dict[int, int | None], list[str]]:
    """Map of live non-kernel PID → start_time_ticks (or None if unreadable).

    Kernel threads are omitted (same as process-census producer). Fail closed with
    None when a non-kernel PID identity cannot be read, or when kernel-vs-userspace
    cannot be decided and the process still appears present without a readable identity.

    When deadline_mono is set, the scan aborts with process_proof_timeout if the
    absolute proof deadline is reached mid-scan (does not authorize cleanup past it).
    Returns (map, reasons); reasons is empty on a full scan or ["process_proof_timeout"].
    """
    out: dict[int, int | None] = {}
    if deadline_mono is not None and time.monotonic() >= deadline_mono:
        return out, ["process_proof_timeout"]
    try:
        names = os.listdir(proc_root)
    except OSError as exc:
        raise ToolError(f"unable to list {proc_root}: {exc}") from exc
    for name in names:
        if deadline_mono is not None and time.monotonic() >= deadline_mono:
            return out, ["process_proof_timeout"]
        if not name.isdigit():
            continue
        pid = int(name)
        pdir = os.path.join(proc_root, name)
        if not os.path.isdir(pdir):
            continue
        try:
            with open(os.path.join(pdir, "stat"), encoding="utf-8") as f:
                start_time_ticks, kernel_thread = parse_stat_identity(f.read())
            if kernel_thread:
                continue
            out[pid] = start_time_ticks
        except (OSError, ValueError, IndexError):
            if _process_still_exists(proc_root, pid):
                # Non-kernel (or undecidable) still present without identity → fail closed.
                out[pid] = None
    if deadline_mono is not None and time.monotonic() >= deadline_mono:
        return out, ["process_proof_timeout"]
    return out, []


def load_snapshot(path: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load a process-census snapshot with an explicit load outcome.

    Open the path directly (no pre-stat / isfile gate). Returns ``(snap, None)``
    on success. On failure ``(None, tag)`` where tag is:

    - ``snapshot_missing`` — only ``FileNotFoundError`` (true absence and
      atomic-replace gaps stay pollable)
    - ``snapshot_malformed`` — JSON parse failure or non-object root (nontransient)
    - ``snapshot_unreadable`` — every other ``OSError``, including
      ``IsADirectoryError`` and ``PermissionError`` on present paths (nontransient;
      must not be polled as missing or retain prior exclusive-transient tags)
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        # Path absent, including atomic-replace gaps: pollable.
        return None, "snapshot_missing"
    except json.JSONDecodeError:
        return None, "snapshot_malformed"
    except OSError:
        # Present but unreadable as a file (directory, permission, etc.).
        return None, "snapshot_unreadable"
    if not isinstance(data, dict):
        return None, "snapshot_malformed"
    return data, None


def snapshot_captured_at(snap: dict[str, Any]) -> datetime | None:
    """Parse snapshot captured_at or return None when missing/invalid (fail closed)."""
    captured = snap.get("captured_at")
    if not isinstance(captured, str):
        return None
    try:
        return parse_iso(captured)
    except (TypeError, ValueError):
        return None


def snapshot_acceptable(
    snap: dict[str, Any],
    *,
    boot_id: str,
    started_at: datetime,
    now: datetime,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if snap.get("schema_version") != 1:
        reasons.append("snapshot_schema")
    if snap.get("complete") is not True:
        reasons.append("snapshot_incomplete")
    if snap.get("boot_id") != boot_id:
        reasons.append("snapshot_boot_mismatch")
    captured = snap.get("captured_at")
    if not isinstance(captured, str):
        reasons.append("snapshot_captured_at_missing")
        return False, reasons
    try:
        cap_dt = parse_iso(captured)
    except (TypeError, ValueError):
        reasons.append("snapshot_captured_at_invalid")
        return False, reasons
    if cap_dt < started_at:
        reasons.append("snapshot_pre_start")
    age = (now - cap_dt).total_seconds()
    if age > SNAPSHOT_MAX_AGE_S or age < -1:
        reasons.append("snapshot_stale")
    if not isinstance(snap.get("processes"), list):
        reasons.append("snapshot_processes_missing")
    return not reasons, reasons


# Wait outcomes that mean "no strictly newer snapshot was observed" (retain prior
# exclusive-transient process_proof reasons at the total deadline).
_NO_NEWER_SNAPSHOT_REASONS = frozenset({"snapshot_not_newer", "snapshot_missing"})


def _reasons_mean_no_newer_snapshot(reasons: list[str]) -> bool:
    """True when wait_for_snapshot ended without observing a strictly newer snapshot."""
    return bool(reasons) and all(r in _NO_NEWER_SNAPSHOT_REASONS for r in reasons)


def _sleep_clamped(poll_s: float, deadline_mono: float) -> None:
    """Sleep at most poll_s and never past the absolute proof deadline."""
    remaining = deadline_mono - time.monotonic()
    if remaining <= 0:
        return
    if poll_s <= 0:
        return
    time.sleep(min(poll_s, remaining))


def wait_for_snapshot(
    census_path: str,
    *,
    proc_root: str,
    started_at: datetime,
    now_fn: Callable[[], datetime],
    wait_s: float,
    poll_s: float,
    min_captured_at: datetime | None = None,
    deadline_mono: float | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Poll until a complete same-boot post-start ≤SNAPSHOT_MAX_AGE_S snapshot appears.

    When min_captured_at is set, also require captured_at strictly newer than that
    timestamp (used after a transient process-proof failure).

    Absolute bound: when ``deadline_mono`` is provided it is the exclusive monotonic
    deadline for checks and clamped sleeps (production path). When omitted, the
    standalone ``wait_s`` API establishes ``monotonic() + wait_s`` *before* boot-id
    / setup work so setup latency cannot extend the window. Zero-wait still does a
    single observation.

    After a transient proof failure (min_captured_at set): if a strictly newer snapshot
    is observed but is incomplete/malformed/stale/otherwise unacceptable, return its
    exact validation reasons immediately (do not poll to the deadline). If the current
    snapshot cannot establish a valid captured_at for the newer-than check, fail closed
    with the validation tags. Load-time ``snapshot_malformed`` / ``snapshot_unreadable``
    always block immediately (including after exclusive-transient); path absence stays
    ``snapshot_missing`` and may be polled (atomic-replace gaps).
    """
    # Establish the absolute deadline before any setup so boot-id I/O cannot extend it.
    if deadline_mono is None:
        deadline_mono = time.monotonic() + max(0.0, wait_s)
    boot_id = read_boot_id(proc_root)
    last_reasons = ["snapshot_missing"]
    while True:
        # Always observe at least once (wait_s=0 still does a single-shot check).
        now = now_fn()
        snap, load_err = load_snapshot(census_path)
        if load_err is not None:
            if load_err == "snapshot_missing":
                last_reasons = ["snapshot_missing"]
            else:
                # Nontransient load failure: block immediately; do not poll.
                return None, [load_err]
        else:
            assert snap is not None
            ok, reasons = snapshot_acceptable(
                snap, boot_id=boot_id, started_at=started_at, now=now
            )
            if min_captured_at is not None:
                cap_dt = snapshot_captured_at(snap)
                if cap_dt is None:
                    # Fail closed: cannot decide strictly-newer vs prior transient.
                    if reasons:
                        return None, list(reasons)
                    captured = snap.get("captured_at")
                    if not isinstance(captured, str):
                        return None, ["snapshot_captured_at_missing"]
                    return None, ["snapshot_captured_at_invalid"]
                if cap_dt > min_captured_at:
                    # Strictly newer snapshot observed — accept or block immediately.
                    if ok:
                        return snap, []
                    return None, list(reasons)
                # Same or older captured_at: keep polling; do not treat as newer.
                last_reasons = ["snapshot_not_newer"]
            elif ok:
                return snap, []
            else:
                last_reasons = reasons
        if time.monotonic() >= deadline_mono:
            return None, last_reasons
        _sleep_clamped(poll_s, deadline_mono)


def reasons_exclusively_transient(reasons: list[str]) -> bool:
    """True only when reasons is non-empty and every tag is a pure process race."""
    return bool(reasons) and all(r in TRANSIENT_PROCESS_REASONS for r in reasons)


def wait_for_process_proof(
    census_path: str,
    proc_root: str,
    required_root: str,
    *extra_required_roots: str,
    started_at: datetime,
    now_fn: Callable[[], datetime],
    wait_s: float,
    poll_s: float,
) -> tuple[bool, list[str], dict[int, dict[str, Any]], dict[str, Any] | None]:
    """Wait for an acceptable snapshot and prove live process identity.

    Single bounded total window (default SNAPSHOT_WAIT_S = 75s) covering the first
    acceptable-snapshot wait, live /proc scans inside process_proof, all sleeps, and
    any exclusive-transient retries. Never accepts a proof that completes after the
    absolute deadline.

    When process_proof fails solely for process_new, process_pid_mismatch, and/or
    live_process_race, wait for a complete same-boot acceptable snapshot whose
    captured_at is strictly newer than the rejected one, then re-run full proof.
    If a strictly newer snapshot arrives but is incomplete/malformed/stale/root-invalid
    or otherwise nontransient, block immediately with that snapshot's exact reasons
    (do not keep the prior exclusive-transient tags). Any other non-retryable reason
    blocks immediately. Persistent exclusive-transient churn with no strictly newer
    snapshot blocks at the deadline with the exact final process_proof reasons.

    Returns (ok, reasons, by_pid, snap_or_None).
    """
    start_mono = time.monotonic()
    wait_s = max(0.0, wait_s)
    deadline = start_mono + wait_s
    # Bound live scans whenever a positive total window is configured. wait_s=0
    # preserves historical single-shot semantics (one full proof, no mid-scan budget).
    scan_deadline: float | None = deadline if wait_s > 0 else None
    last_reasons: list[str] = ["snapshot_missing"]
    by_pid: dict[int, dict[str, Any]] = {}
    last_snap: dict[str, Any] | None = None
    min_captured_at: datetime | None = None
    attempted = False

    while True:
        now_mono = time.monotonic()
        if attempted and now_mono >= deadline:
            return False, last_reasons, by_pid, last_snap

        remaining = max(0.0, deadline - now_mono)
        # Always pass the original absolute deadline for positive windows so setup
        # work inside wait_for_snapshot cannot rebase monotonic()+remaining.
        snap, snap_reasons = wait_for_snapshot(
            census_path,
            proc_root=proc_root,
            started_at=started_at,
            now_fn=now_fn,
            wait_s=remaining,
            poll_s=poll_s,
            min_captured_at=min_captured_at,
            deadline_mono=deadline if wait_s > 0 else None,
        )
        attempted = True

        if snap is None:
            # Retain prior exclusive-transient process_proof reasons only when no
            # strictly newer snapshot was ever observed (missing / not-newer).
            # A newer invalid snapshot's exact validation tags must surface instead.
            if (
                min_captured_at is not None
                and reasons_exclusively_transient(last_reasons)
                and _reasons_mean_no_newer_snapshot(snap_reasons)
            ):
                return False, last_reasons, by_pid, last_snap
            return False, list(snap_reasons) or last_reasons, by_pid, last_snap

        # Positive window only: refuse to start a scan after the absolute deadline.
        if scan_deadline is not None and time.monotonic() >= scan_deadline:
            return False, ["process_proof_timeout"], by_pid, snap

        ok, reasons, by_pid = process_proof(
            snap,
            proc_root,
            required_root,
            *extra_required_roots,
            deadline_mono=scan_deadline,
        )
        last_snap = snap
        if ok:
            # M2: never authorize cleanup after the absolute deadline, and re-check
            # snapshot acceptability/freshness with current now_fn before success.
            if scan_deadline is not None and time.monotonic() >= scan_deadline:
                return False, ["process_proof_timeout"], by_pid, snap
            boot_id = read_boot_id(proc_root)
            still_ok, fresh_reasons = snapshot_acceptable(
                snap,
                boot_id=boot_id,
                started_at=started_at,
                now=now_fn(),
            )
            if not still_ok:
                return False, list(fresh_reasons), by_pid, snap
            if scan_deadline is not None and time.monotonic() >= scan_deadline:
                return False, ["process_proof_timeout"], by_pid, snap
            return True, [], by_pid, snap

        last_reasons = list(reasons)
        if not reasons_exclusively_transient(reasons):
            return False, last_reasons, by_pid, snap

        # Exclusive transient race: require a strictly newer acceptable snapshot.
        cap_dt = snapshot_captured_at(snap)
        if cap_dt is None:
            # Fail closed: cannot establish the exclusive-retry floor.
            captured = snap.get("captured_at")
            if not isinstance(captured, str):
                return False, ["snapshot_captured_at_missing"], by_pid, snap
            return False, ["snapshot_captured_at_invalid"], by_pid, snap
        min_captured_at = cap_dt

        if time.monotonic() >= deadline:
            return False, last_reasons, by_pid, last_snap

        _sleep_clamped(poll_s, deadline)


def _well_formed_reference(ref: Any) -> bool:
    if not isinstance(ref, dict):
        return False
    kind = ref.get("kind")
    path = ref.get("path")
    if kind not in REF_KINDS:
        return False
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return False
    return True


def snapshot_roots_cover(
    snap: dict[str, Any],
    required_path: str,
    *extra_required_paths: str,
) -> tuple[bool, list[str]]:
    """Require well-formed absolute roots that cover every required path."""
    reasons: list[str] = []
    roots = snap.get("roots")
    if not isinstance(roots, list):
        return False, ["snapshot_roots_missing"]
    if not roots:
        return False, ["snapshot_roots_empty"]
    seen: set[str] = set()
    norm_roots: list[str] = []
    for raw in roots:
        if not isinstance(raw, str) or not raw.strip():
            reasons.append("snapshot_roots_malformed")
            continue
        root = raw.strip()
        if not os.path.isabs(root):
            reasons.append("snapshot_roots_malformed")
            continue
        root_n = norm(root)
        if root_n in seen:
            reasons.append("snapshot_roots_duplicate")
            continue
        seen.add(root_n)
        # Reject symlinked census root paths when they exist on disk.
        try:
            ensure_no_symlink_components(root_n, "snapshot root")
        except ToolError:
            reasons.append("snapshot_roots_symlink")
            continue
        norm_roots.append(root_n)
    if reasons:
        # Deduplicate reason tags while preserving order.
        out: list[str] = []
        seen_r: set[str] = set()
        for r in reasons:
            if r not in seen_r:
                seen_r.add(r)
                out.append(r)
        return False, out
    if not norm_roots:
        return False, ["snapshot_roots_empty"]
    for req in (required_path, *extra_required_paths):
        rr = norm(req)
        if not any(under(rr, r) for r in norm_roots):
            return False, ["snapshot_roots_unrelated"]
    return True, []


def process_proof(
    snap: dict[str, Any],
    proc_root: str,
    runtime_root: str,
    *extra_required_roots: str,
    deadline_mono: float | None = None,
) -> tuple[bool, list[str], dict[int, dict[str, Any]]]:
    """Every live non-kernel pid matches snapshot pid+start_time; every record complete.

    When deadline_mono is set, live /proc scans are bounded by that absolute monotonic
    deadline and return process_proof_timeout (non-transient) instead of authorizing
    a match after the proof window.
    """
    reasons: list[str] = []
    if deadline_mono is not None and time.monotonic() >= deadline_mono:
        return False, ["process_proof_timeout"], {}

    roots_ok, roots_reasons = snapshot_roots_cover(
        snap, runtime_root, *extra_required_roots
    )
    if not roots_ok:
        reasons.extend(roots_reasons)

    by_pid: dict[int, dict[str, Any]] = {}
    for rec in snap.get("processes") or []:
        if not isinstance(rec, dict):
            reasons.append("snapshot_process_malformed")
            continue
        pid = rec.get("pid")
        st = rec.get("start_time_ticks")
        if not isinstance(pid, int) or not isinstance(st, int):
            reasons.append("snapshot_process_fields")
            continue
        if pid in by_pid:
            reasons.append("snapshot_process_duplicate")
            continue
        if rec.get("scope_complete") is not True:
            reasons.append("snapshot_scope_incomplete")
            continue
        refs = rec.get("references")
        if not isinstance(refs, list):
            reasons.append("snapshot_references_malformed")
            continue
        if any(not _well_formed_reference(r) for r in refs):
            reasons.append("snapshot_references_malformed")
            continue
        by_pid[pid] = rec

    if deadline_mono is not None and time.monotonic() >= deadline_mono:
        return False, ["process_proof_timeout"], by_pid

    live, live_timeout = live_pid_map(proc_root, deadline_mono=deadline_mono)
    if live_timeout:
        reasons.extend(live_timeout)
        seen: set[str] = set()
        uniq: list[str] = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        return False, uniq, by_pid

    for pid, live_st in live.items():
        if live_st is None:
            reasons.append("live_process_unreadable")
            continue
        if pid not in by_pid:
            reasons.append("process_new")
            continue
        if by_pid[pid].get("start_time_ticks") != live_st:
            reasons.append("process_pid_mismatch")

    if deadline_mono is not None and time.monotonic() >= deadline_mono:
        reasons.append("process_proof_timeout")
        seen = set()
        uniq = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        return False, uniq, by_pid

    # Re-confirm identity after live map (post-start proof is already gated by snapshot).
    live2, live2_timeout = live_pid_map(proc_root, deadline_mono=deadline_mono)
    if live2_timeout:
        reasons.extend(live2_timeout)
    elif live2 != live:
        reasons.append("live_process_race")
    else:
        for pid, live_st in live2.items():
            if live_st is None or pid not in by_pid or by_pid[pid].get("start_time_ticks") != live_st:
                if "live_process_race" not in reasons and "live_process_unreadable" not in reasons:
                    if live_st is None:
                        reasons.append("live_process_unreadable")
                    elif pid not in by_pid:
                        reasons.append("process_new")
                    else:
                        reasons.append("process_pid_mismatch")

    if deadline_mono is not None and time.monotonic() >= deadline_mono:
        if "process_proof_timeout" not in reasons:
            reasons.append("process_proof_timeout")

    seen = set()
    uniq = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return not uniq, uniq, by_pid


def processes_touching_scratch(
    by_pid: dict[int, dict[str, Any]], scratch_dir: str
) -> list[int]:
    hits: list[int] = []
    for pid, rec in by_pid.items():
        refs = rec.get("references") or []
        for ref in refs:
            path = ref.get("path") if isinstance(ref, dict) else None
            if isinstance(path, str) and under(path, scratch_dir):
                hits.append(pid)
                break
    return hits


# ---------------------------------------------------------------------------
# Deterministic tree inventory (candidate identity + artifacts)
# ---------------------------------------------------------------------------


def file_sha256(path: str, deadline: float) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if time.monotonic() > deadline:
                raise ToolError("inventory_timeout")
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def tree_inventory(root: str, deadline: float) -> tuple[list[dict[str, Any]], list[str]]:
    """Full deterministic inventory. Never follows symlinks. Bound by deadline."""
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    try:
        root_st = os.lstat(root)
    except OSError:
        return [], ["root_missing"]
    if stat.S_ISLNK(root_st.st_mode):
        return [], ["root_is_symlink"]
    if not stat.S_ISDIR(root_st.st_mode):
        return [], ["root_not_directory"]
    root_dev = root_st.st_dev
    root_n = norm(root)

    def rel_of(path: str) -> str:
        if norm(path) == root_n:
            return "."
        return os.path.relpath(path, root_n)

    def rec_for(path: str, st: os.stat_result, typ: str, digest: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": typ,
            "rel": rel_of(path),
            "dev": st.st_dev,
            "ino": st.st_ino,
            "mode": stat.S_IMODE(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "size": st.st_size if typ == "file" else 0,
            "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
            "nlink": st.st_nlink,
        }
        if typ == "file":
            out["sha256"] = digest
        return out

    def walk(path: str) -> None:
        if time.monotonic() > deadline:
            errors.append("inventory_timeout")
            return
        try:
            st = os.lstat(path)
        except OSError:
            errors.append("path_changed")
            return
        if st.st_dev != root_dev:
            errors.append("mount_crossing")
            return
        if stat.S_ISLNK(st.st_mode):
            errors.append("symlink_content")
            return
        if stat.S_ISREG(st.st_mode):
            if st.st_nlink > 1:
                errors.append("hard_link")
                return
            try:
                digest = file_sha256(path, deadline)
            except ToolError as exc:
                errors.append(str(exc) if str(exc) == "inventory_timeout" else "unreadable_file")
                return
            except OSError:
                errors.append("unreadable_file")
                return
            # Detect change between lstat and hash
            try:
                st2 = os.lstat(path)
            except OSError:
                errors.append("path_changed")
                return
            if (
                st2.st_ino != st.st_ino
                or st2.st_dev != st.st_dev
                or st2.st_size != st.st_size
                or int(getattr(st2, "st_mtime_ns", int(st2.st_mtime * 1e9)))
                != int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
            ):
                errors.append("path_changed")
                return
            records.append(rec_for(path, st, "file", digest))
            return
        if stat.S_ISDIR(st.st_mode):
            records.append(rec_for(path, st, "dir"))
            try:
                with os.scandir(path) as it:
                    names = sorted(ent.name for ent in it)
            except OSError:
                errors.append("unreadable_dir")
                return
            for name in names:
                if time.monotonic() > deadline:
                    errors.append("inventory_timeout")
                    return
                walk(os.path.join(path, name))
            return
        errors.append("special_file")

    walk(root)
    records.sort(key=lambda r: (r["rel"], r["type"]))
    return records, errors


def inventory_fingerprint(records: list[dict[str, Any]]) -> str:
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_token(
    agent_id: str,
    generation: str,
    released_at: str,
    inventory_fp: str,
) -> str:
    payload = "|".join([agent_id, generation, released_at, inventory_fp])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def inventories_match(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    return inventory_fingerprint(a) == inventory_fingerprint(b)


# ---------------------------------------------------------------------------
# Scratch listing / classification / probe
# ---------------------------------------------------------------------------


def list_scratch_entries(runtime_root: str) -> tuple[list[str], list[str]]:
    """Return (uuid_ids, unknown_names). Unknowns make inventory incomplete."""
    scratch_root = os.path.join(runtime_root, "scratch")
    ensure_no_symlink_components(scratch_root, "scratch root")
    if not os.path.lexists(scratch_root):
        return [], []
    if os.path.islink(scratch_root) or not os.path.isdir(scratch_root):
        raise ToolError(f"scratch root is not a real directory: {scratch_root}")
    ids: list[str] = []
    unknown: list[str] = []
    try:
        for name in sorted(os.listdir(scratch_root)):
            if is_uuid(name):
                ids.append(name)
            else:
                unknown.append(name)
    except OSError as exc:
        raise ToolError(f"cannot list scratch root: {exc}") from exc
    return ids, unknown


def classify_candidate(
    *,
    agent_id: str,
    runtime_root: str,
    now: datetime,
    paseo: dict[str, Any],
    runner: Callable[[list[str]], tuple[int, str, str]],
    process_ok: bool,
    process_reasons: list[str],
    by_pid: dict[int, dict[str, Any]],
    size_deadline: float,
    holding_lock_for: str | None = None,
    inventory_incomplete: bool = False,
    inventory_reasons: list[str] | None = None,
) -> dict[str, Any]:
    scratch_dir = os.path.join(runtime_root, "scratch", agent_id)
    out: dict[str, Any] = {
        "agent_id": agent_id,
        "path": scratch_dir,
        "classification": "blocked",
        "reasons": [],
        "size_bytes": None,
        "generation": None,
        "released_at": None,
        "candidate_token": None,
        "inventory_fingerprint": None,
    }
    reasons: list[str] = []

    if inventory_incomplete:
        reasons.extend(inventory_reasons or ["scratch_inventory_incomplete"])
        out["reasons"] = reasons
        return out

    if not process_ok:
        reasons.extend(process_reasons or ["process_census_blocked"])
        out["reasons"] = reasons
        return out

    hint, man_reasons, manifest = classify_manifest(scratch_dir, agent_id, now)
    reasons.extend(man_reasons)

    size, size_err = dir_size_capped(scratch_dir, size_deadline)
    if size_err:
        reasons.append(size_err)
        out["reasons"] = reasons
        return out
    out["size_bytes"] = size

    if holding_lock_for != agent_id:
        held, lock_reason = lock_present(runtime_root, agent_id)
        if held:
            reasons.append(lock_reason or "lock_present")
            out["reasons"] = reasons
            out["classification"] = "protected"
            return out

    try:
        info = inspect_agent(runner, agent_id)
    except ToolError as exc:
        msg = str(exc).lower()
        if "not found" in msg or "exit" in msg:
            reasons.append("agent_unknown")
        else:
            reasons.append("agent_inspect_failed")
        out["reasons"] = reasons
        return out

    ok_arch, arch_reasons = agent_is_archived_or_closed(info)
    if not ok_arch:
        reasons.extend(arch_reasons)
        out["reasons"] = reasons
        out["classification"] = "protected"
        return out

    if agent_id in paseo["unarchived_ids"]:
        reasons.append("agent_unarchived")
        out["reasons"] = reasons
        out["classification"] = "protected"
        return out

    if has_unarchived_descendant(agent_id, paseo["parent_of"], paseo["unarchived_ids"]):
        reasons.append("unarchived_descendant")
        out["reasons"] = reasons
        out["classification"] = "protected"
        return out

    if agent_id in paseo.get("protected_schedule_agents", set()):
        reasons.append("active_schedule")
        out["reasons"] = reasons
        out["classification"] = "protected"
        return out

    for perm in paseo["permits"]:
        if isinstance(perm, dict) and permit_for_agent(perm, agent_id):
            reasons.append("pending_permission")
            out["reasons"] = reasons
            out["classification"] = "protected"
            return out

    try:
        for term in paseo["terminals"]:
            if isinstance(term, dict) and terminal_in_scratch(term, scratch_dir):
                reasons.append("terminal_in_scratch")
                out["reasons"] = reasons
                out["classification"] = "protected"
                return out
    except ToolError:
        reasons.append("terminal_cwd_ambiguous")
        out["reasons"] = reasons
        return out

    pids = processes_touching_scratch(by_pid, scratch_dir)
    if pids:
        reasons.append("process_in_scratch")
        out["reasons"] = reasons
        out["classification"] = "protected"
        return out

    if hint != "ok" or manifest is None:
        out["reasons"] = reasons or man_reasons or ["manifest_blocked"]
        out["classification"] = "blocked" if hint == "blocked" else "protected"
        if manifest:
            out["generation"] = manifest.get("generation")
            out["released_at"] = manifest.get("releasedAt")
        return out

    inv_deadline = time.monotonic() + INVENTORY_TIMEOUT_S
    records, inv_errors = tree_inventory(scratch_dir, inv_deadline)
    if inv_errors:
        reasons.extend(sorted(set(inv_errors)))
        out["reasons"] = reasons
        return out
    # Second matching inventory for probe-time identity stability.
    records2, inv_errors2 = tree_inventory(scratch_dir, inv_deadline)
    if inv_errors2:
        reasons.extend(sorted(set(inv_errors2)))
        out["reasons"] = reasons
        return out
    if not inventories_match(records, records2):
        reasons.append("tree_changed")
        out["reasons"] = reasons
        return out

    gen = str(manifest["generation"])
    released_at = str(manifest["releasedAt"])
    fp = inventory_fingerprint(records2)
    out["generation"] = gen
    out["released_at"] = released_at
    out["inventory_fingerprint"] = fp
    out["candidate_token"] = make_token(agent_id, gen, released_at, fp)
    out["classification"] = "eligible"
    out["reasons"] = []
    return out


def run_probe(
    *,
    config_path: str,
    census_path: str,
    proc_root: str,
    runner: Callable[[list[str]], tuple[int, str, str]],
    now_fn: Callable[[], datetime],
    wait_s: float = SNAPSHOT_WAIT_S,
    poll_s: float = 0.25,
    started_at: datetime | None = None,
    holding_lock_for: str | None = None,
) -> dict[str, Any]:
    started = started_at or now_fn()
    started_iso = started.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    runtime_root = load_runtime_root(config_path)
    for name in ("scratch", "locks", "artifacts", "quarantine"):
        p = os.path.join(runtime_root, name)
        if os.path.lexists(p):
            ensure_no_symlink_components(p, name)
    size_deadline = time.monotonic() + SIZE_WALK_TIMEOUT_S

    free = {
        "root": free_bytes("/"),
        "runtime_root": free_bytes(runtime_root),
    }
    report = {
        "artifacts": report_aggregate(runtime_root, "artifacts", size_deadline),
        "quarantine": report_aggregate(runtime_root, "quarantine", size_deadline),
    }
    report_blockers: list[str] = []
    for key, agg in report.items():
        if agg["status"] != "ok":
            report_blockers.append(f"report_{key}_{agg['error'] or 'unknown'}")

    process_ok, process_reasons, by_pid, snap = wait_for_process_proof(
        census_path,
        proc_root,
        runtime_root,
        started_at=started,
        now_fn=now_fn,
        wait_s=wait_s,
        poll_s=poll_s,
    )

    paseo_ok = True
    paseo_reasons: list[str] = []
    paseo: dict[str, Any] = {
        "unarchived_ids": set(),
        "unarchived": [],
        "schedules": [],
        "permits": [],
        "terminals": [],
        "protected_schedule_agents": set(),
        "global_schedule_blocks": [],
        "parent_of": {},
    }
    try:
        paseo = collect_paseo_census(runner)
    except ToolError as exc:
        paseo_ok = False
        paseo_reasons.append(f"paseo_census:{exc}")

    if not paseo_ok:
        process_ok = False
        process_reasons = process_reasons + paseo_reasons

    global_schedule_blocks = paseo.get("global_schedule_blocks") or []
    if global_schedule_blocks:
        # Active new-agent (or unknown/malformed) schedules omit bound cwd; block all.
        process_ok = False
        process_reasons = process_reasons + list(global_schedule_blocks)

    if report_blockers:
        # Aggregate timeout/unreadable: bytes null + exact error, and block every
        # scratch candidate for this wake (no silent pass-through).
        process_ok = False
        process_reasons = process_reasons + report_blockers

    uuid_ids, unknown_names = list_scratch_entries(runtime_root)
    inventory_incomplete = bool(unknown_names)
    inventory_reasons = (
        [f"unknown_scratch_entry:{n}" for n in unknown_names] if unknown_names else []
    )

    now = now_fn()
    candidates = []
    for agent_id in uuid_ids:
        candidates.append(
            classify_candidate(
                agent_id=agent_id,
                runtime_root=runtime_root,
                now=now,
                paseo=paseo,
                runner=runner,
                process_ok=process_ok,
                process_reasons=process_reasons,
                by_pid=by_pid,
                size_deadline=size_deadline,
                holding_lock_for=holding_lock_for,
                inventory_incomplete=inventory_incomplete,
                inventory_reasons=inventory_reasons,
            )
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_iso,
        "runtime_root": runtime_root,
        "free_bytes": free,
        "report_only": {
            "artifacts_bytes": report["artifacts"]["bytes"],
            "quarantine_bytes": report["quarantine"]["bytes"],
            "artifacts_status": report["artifacts"]["status"],
            "quarantine_status": report["quarantine"]["status"],
            "artifacts_error": report["artifacts"]["error"],
            "quarantine_error": report["quarantine"]["error"],
            "errors": report_blockers,
        },
        "scratch_inventory": {
            "complete": not inventory_incomplete,
            "unknown_entries": unknown_names,
        },
        "process_census": {
            "status": "ok" if process_ok else "blocked",
            "reasons": process_reasons,
            "captured_at": None if snap is None else snap.get("captured_at"),
            "boot_id": None if snap is None else snap.get("boot_id"),
        },
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# Archive: final inventory match → tombstone rename → exact remove
# ---------------------------------------------------------------------------


def find_eligible(
    probe: dict[str, Any], agent_id: str, generation: str, token: str
) -> dict[str, Any] | None:
    for c in probe.get("candidates") or []:
        if (
            c.get("agent_id") == agent_id
            and c.get("classification") == "eligible"
            and c.get("generation") == generation
            and c.get("candidate_token") == token
        ):
            return c
    return None


def ensure_quarantine_released(runtime_root: str) -> str:
    ensure_no_symlink_components(runtime_root, "runtimeRoot")
    q = os.path.join(runtime_root, "quarantine")
    if os.path.lexists(q):
        ensure_no_symlink_components(q, "quarantine")
        if os.path.islink(q) or not os.path.isdir(q):
            raise ToolError(f"quarantine is not a real directory: {q}")
    else:
        mkdir_exact(q, 0o700)
    rel = os.path.join(runtime_root, TOMBSTONE_REL)
    if os.path.lexists(rel):
        ensure_no_symlink_components(rel, "released-scratch")
        if os.path.islink(rel) or not os.path.isdir(rel):
            raise ToolError(f"released-scratch is not a real directory: {rel}")
    else:
        mkdir_exact(rel, 0o700)
    return rel


def tombstone_name(agent_id: str, generation: str, token: str) -> str:
    # Unique, non-colliding with relaunch scratch/{agent_id}.
    return f"{agent_id}.{generation}.{token[:16]}.{time.time_ns()}"


def remove_tombstone_exact(tombstone_dir: str, expected: os.stat_result) -> int:
    """Remove only the exact tombstone directory. Symlink-safe, fail-closed."""
    try:
        st0 = os.lstat(tombstone_dir)
    except OSError as exc:
        raise ToolError(f"tombstone missing before removal: {tombstone_dir}") from exc
    if (
        st0.st_ino != expected.st_ino
        or st0.st_dev != expected.st_dev
        or not stat.S_ISDIR(st0.st_mode)
        or stat.S_ISLNK(st0.st_mode)
    ):
        raise ToolError(f"tombstone identity mismatch: {tombstone_dir}")

    inv_deadline = time.monotonic() + INVENTORY_TIMEOUT_S
    records, errors = tree_inventory(tombstone_dir, inv_deadline)
    if errors:
        raise ToolError(f"refuse tombstone removal: {','.join(sorted(set(errors)))}")

    files = [r for r in records if r["type"] == "file"]
    # deepest dirs first; root "." last
    dirs = [r for r in records if r["type"] == "dir"]
    dirs.sort(
        key=lambda r: (1 if r["rel"] == "." else 0, -r["rel"].count(os.sep), r["rel"])
    )

    bytes_removed = 0
    for rec in files:
        path = tombstone_dir if rec["rel"] == "." else os.path.join(tombstone_dir, rec["rel"])
        try:
            st1 = os.lstat(path)
        except OSError as exc:
            raise ToolError(f"path changed during verification: {path}") from exc
        if (
            st1.st_ino != rec["ino"]
            or st1.st_dev != rec["dev"]
            or st1.st_size != rec["size"]
            or st1.st_nlink > 1
            or not stat.S_ISREG(st1.st_mode)
        ):
            raise ToolError(f"contents changed during verification: {path}")
        bytes_removed += st1.st_size
        os.unlink(path)

    for rec in dirs:
        path = tombstone_dir if rec["rel"] == "." else os.path.join(tombstone_dir, rec["rel"])
        try:
            st1 = os.lstat(path)
        except OSError as exc:
            raise ToolError(f"path changed during verification: {path}") from exc
        if st1.st_ino != rec["ino"] or st1.st_dev != rec["dev"] or not stat.S_ISDIR(st1.st_mode):
            raise ToolError(f"directory identity changed: {path}")
        try:
            remaining = os.listdir(path)
        except OSError as exc:
            raise ToolError(f"cannot list before rmdir: {path}") from exc
        if remaining:
            raise ToolError(f"directory not empty before rmdir: {path}")
        os.rmdir(path)

    if os.path.lexists(tombstone_dir):
        raise ToolError(f"tombstone still exists after removal: {tombstone_dir}")
    return bytes_removed


def run_archive(
    *,
    config_path: str,
    agent_id: str,
    generation: str,
    candidate_token: str,
    census_path: str,
    proc_root: str,
    runner: Callable[[list[str]], tuple[int, str, str]],
    now_fn: Callable[[], datetime],
    wait_s: float = SNAPSHOT_WAIT_S,
    poll_s: float = 0.25,
) -> dict[str, Any]:
    if not is_uuid(agent_id):
        raise ToolError("agent-id must be a UUID")
    if not is_uuid(generation):
        raise ToolError("generation must be a UUID")
    if not candidate_token or not isinstance(candidate_token, str):
        raise ToolError("candidate-token is required")

    runtime_root = load_runtime_root(config_path)
    scratch_dir = os.path.join(runtime_root, "scratch", agent_id)
    artifacts_dir = os.path.join(runtime_root, "artifacts", agent_id)
    ensure_no_symlink_components(scratch_dir, "scratch")
    if os.path.lexists(artifacts_dir):
        ensure_no_symlink_components(artifacts_dir, "artifacts")

    # Artifact inventory (bounded; streaming hashes — not full content in memory).
    art_before: list[dict[str, Any]] | None = None
    art_fp_before: str | None = None
    if os.path.isdir(artifacts_dir) and not os.path.islink(artifacts_dir):
        inv_deadline = time.monotonic() + INVENTORY_TIMEOUT_S
        art_before, art_errs = tree_inventory(artifacts_dir, inv_deadline)
        if art_errs:
            raise ToolError(f"artifacts inventory failed: {','.join(sorted(set(art_errs)))}")
        art_fp_before = inventory_fingerprint(art_before)

    free_before = {
        "root": free_bytes("/"),
        "runtime_root": free_bytes(runtime_root),
    }

    lock_dir, lock_token = acquire_lock(runtime_root, agent_id)
    outcome: dict[str, Any] | None = None
    try:
        # Final matching proof after fresh census + lock.
        started = now_fn()
        probe = run_probe(
            config_path=config_path,
            census_path=census_path,
            proc_root=proc_root,
            runner=runner,
            now_fn=now_fn,
            wait_s=wait_s,
            poll_s=poll_s,
            started_at=started,
            holding_lock_for=agent_id,
        )
        cand = find_eligible(probe, agent_id, generation, candidate_token)
        if cand is None:
            raise ToolError(
                "archive revalidation failed: candidate not eligible or token/generation mismatch"
            )
        if norm(cand["path"]) != norm(scratch_dir):
            raise ToolError("archive revalidation failed: path mismatch")

        # Final inventory after census/lock must still match the candidate token.
        inv_deadline = time.monotonic() + INVENTORY_TIMEOUT_S
        final_records, final_errs = tree_inventory(scratch_dir, inv_deadline)
        if final_errs:
            raise ToolError(f"final inventory blocked: {','.join(sorted(set(final_errs)))}")
        final_fp = inventory_fingerprint(final_records)
        expected_token = make_token(agent_id, generation, str(cand["released_at"]), final_fp)
        if expected_token != candidate_token:
            raise ToolError("final inventory does not match candidate token")

        try:
            scratch_st = os.lstat(scratch_dir)
        except OSError as exc:
            raise ToolError(f"cannot lstat scratch before rename: {scratch_dir}") from exc
        if not stat.S_ISDIR(scratch_st.st_mode) or stat.S_ISLNK(scratch_st.st_mode):
            raise ToolError(f"scratch is not a real directory: {scratch_dir}")

        q_parent = ensure_quarantine_released(runtime_root)
        # Same-filesystem atomic rename into private tombstone.
        tname = tombstone_name(agent_id, generation, candidate_token)
        tombstone_dir = os.path.join(q_parent, tname)
        ensure_no_symlink_components(tombstone_dir, "tombstone")
        if os.path.lexists(tombstone_dir):
            raise ToolError(f"tombstone path already exists (collision): {tombstone_dir}")
        try:
            os.rename(scratch_dir, tombstone_dir)
        except OSError as exc:
            raise ToolError(f"atomic rename to tombstone failed: {exc}") from exc

        try:
            t_st = os.lstat(tombstone_dir)
        except OSError as exc:
            raise ToolError(f"tombstone missing after rename: {tombstone_dir}") from exc
        if t_st.st_ino != scratch_st.st_ino or t_st.st_dev != scratch_st.st_dev:
            raise ToolError(f"tombstone identity mismatch after rename: {tombstone_dir}")

        try:
            bytes_removed = remove_tombstone_exact(tombstone_dir, t_st)
        except Exception as exc:
            # Do not pretend completion; leave recoverable tombstone, keep lock until outcome.
            outcome = {
                "schema_version": SCHEMA_VERSION,
                "status": "quarantined_pending_removal",
                "agent_id": agent_id,
                "generation": generation,
                "path": scratch_dir,
                "tombstone_path": tombstone_dir,
                "bytes_removed": 0,
                "error": str(exc),
                "free_bytes_before": free_before,
                "free_bytes_after": {
                    "root": free_bytes("/"),
                    "runtime_root": free_bytes(runtime_root),
                },
                "artifacts_preserved": True,
            }
            return outcome

        # Artifacts must be untouched (inventory match, not full content reload).
        if art_fp_before is not None and art_before is not None:
            inv_deadline = time.monotonic() + INVENTORY_TIMEOUT_S
            art_after, art_errs = tree_inventory(artifacts_dir, inv_deadline)
            if art_errs:
                raise ToolError(
                    f"artifacts inventory after archive failed: {','.join(sorted(set(art_errs)))}"
                )
            if inventory_fingerprint(art_after) != art_fp_before:
                raise ToolError("artifacts mutated during archive")

        free_after = {
            "root": free_bytes("/"),
            "runtime_root": free_bytes(runtime_root),
        }
        outcome = {
            "schema_version": SCHEMA_VERSION,
            "status": "archived",
            "agent_id": agent_id,
            "generation": generation,
            "path": scratch_dir,
            "tombstone_path": None,
            "bytes_removed": bytes_removed,
            "free_bytes_before": free_before,
            "free_bytes_after": free_after,
            "artifacts_preserved": True,
        }
        return outcome
    finally:
        # Lock held until outcome is fully determined; release after return path is set.
        release_lock(lock_dir, agent_id, lock_token)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fail-closed managed agent-scratch probe and exact archive."
    )
    p.add_argument(
        "--config",
        default=os.path.expanduser("~/.paseo/config.json"),
        help="Paseo config.json path (reads agents.runtimeRoot).",
    )
    p.add_argument(
        "--paseo-bin",
        default=os.environ.get("PASEO_BIN", "paseo"),
        help="Paseo CLI binary for read-only census.",
    )
    p.add_argument(
        "--census-path",
        default=DEFAULT_CENSUS,
        help="Process census snapshot path.",
    )
    p.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    p.add_argument("--wait-seconds", type=float, default=SNAPSHOT_WAIT_S, help=argparse.SUPPRESS)
    p.add_argument("--poll-seconds", type=float, default=0.25, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="Classify scratch candidates (no mutation).")
    arch = sub.add_parser("archive", help="Archive exactly one eligible candidate.")
    arch.add_argument("--agent-id", required=True)
    arch.add_argument("--generation", required=True)
    arch.add_argument("--candidate-token", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = default_paseo_runner(args.paseo_bin)
    now_fn = lambda: datetime.now(timezone.utc)

    try:
        if args.command == "probe":
            result = run_probe(
                config_path=args.config,
                census_path=args.census_path,
                proc_root=args.proc_root,
                runner=runner,
                now_fn=now_fn,
                wait_s=args.wait_seconds,
                poll_s=args.poll_seconds,
            )
        elif args.command == "archive":
            result = run_archive(
                config_path=args.config,
                agent_id=args.agent_id,
                generation=args.generation,
                candidate_token=args.candidate_token,
                census_path=args.census_path,
                proc_root=args.proc_root,
                runner=runner,
                now_fn=now_fn,
                wait_s=args.wait_seconds,
                poll_s=args.poll_seconds,
            )
        else:
            raise ToolError(f"unknown command {args.command}")
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except ToolError as exc:
        print(f"agent-scratch-cleanup: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"agent-scratch-cleanup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
