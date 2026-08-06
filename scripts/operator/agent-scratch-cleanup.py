#!/usr/bin/env python3
"""Fail-closed managed agent-scratch probe + exact UUID archive."""

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
SNAPSHOT_MAX_AGE_S = 45
SNAPSHOT_WAIT_S = 45
DEFAULT_CENSUS = "/run/paseo/process-census.json"
PARENT_LABEL = "paseo.parent-agent-id"
LOCK_OP = "archive_scratch"
MANIFEST_NAME = "manifest.json"
OWNER_NAME = "owner.json"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)

class ToolError(Exception):
    """Fatal probe/archive error."""


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
    return p == r or p.startswith(r + os.sep)

def free_bytes(path: str) -> int:
    """statvfs free bytes for path or nearest existing ancestor."""
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

def atomic_write(path: str, data: str, mode: int = 0o600) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{time.time_ns()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp and os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


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
    root = root.strip()
    require_abs(root, "agents.runtimeRoot")
    if os.path.islink(root):
        raise ToolError(f"runtimeRoot must not be a symlink: {root}")
    if not os.path.isdir(root):
        raise ToolError(f"runtimeRoot is not a directory: {root}")
    return norm(root)

def dir_size_capped(path: str, deadline: float) -> tuple[int | None, str | None]:
    """Non-following size walk. Returns (bytes, error_reason)."""
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

def report_aggregate(runtime_root: str, name: str, deadline: float) -> int:
    path = os.path.join(runtime_root, name)
    size, _err = dir_size_capped(path, deadline)
    return 0 if size is None else size


def read_json_file(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def classify_manifest(
    scratch_dir: str, agent_id: str, now: datetime
) -> tuple[str, list[str], dict[str, Any] | None]:
    """Return (classification_hint, reasons, manifest_or_None).

    classification_hint is blocked/protected-ish pre-check only; caller merges.
    """
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
    age = (now - rel_dt).total_seconds()
    if age < RELEASE_GRACE_S:
        return "protected", ["release_grace"], raw
    return "ok", [], raw

def lock_dir_for(runtime_root: str, agent_id: str) -> str:
    return os.path.join(runtime_root, "locks", f"{agent_id}.lock")

def lock_present(runtime_root: str, agent_id: str) -> tuple[bool, str | None]:
    ld = lock_dir_for(runtime_root, agent_id)
    if not os.path.lexists(ld):
        return False, None
    if os.path.islink(ld):
        return True, "lock_is_symlink"
    return True, "lock_present"

def acquire_lock(runtime_root: str, agent_id: str) -> tuple[str, str]:
    """mkdir lock + owner.json. Returns (lock_dir, lock_token). Never breaks by age."""
    locks_parent = os.path.join(runtime_root, "locks")
    os.makedirs(locks_parent, mode=0o700, exist_ok=True)
    ld = lock_dir_for(runtime_root, agent_id)
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
    except OSError:
        pass
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
    if os.path.islink(lock_dir):
        raise ToolError(f"cannot release agent lock: lock path is a symlink: {lock_dir}")
    if not os.path.isdir(lock_dir):
        raise ToolError(f"cannot release agent lock: lock path is not a directory: {lock_dir}")
    owner_path = os.path.join(lock_dir, OWNER_NAME)
    try:
        owner = read_json_file(owner_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"cannot release agent lock: owner.json missing or unreadable for {agent_id}") from exc
    if not isinstance(owner, dict):
        raise ToolError(f"cannot release agent lock: invalid owner.json for {agent_id}")
    if owner.get("agentId") != agent_id or owner.get("lockToken") != lock_token:
        raise ToolError(f"cannot release agent lock: token mismatch for {agent_id}")
    try:
        entries = os.listdir(lock_dir)
    except OSError as exc:
        raise ToolError(f"cannot release agent lock: failed to list lock directory for {agent_id}") from exc
    if entries != [OWNER_NAME] and set(entries) != {OWNER_NAME}:
        if len(entries) != 1 or entries[0] != OWNER_NAME:
            raise ToolError(
                f"cannot release agent lock: unexpected contents in lock directory for {agent_id}"
            )
    try:
        os.unlink(owner_path)
    except OSError as exc:
        raise ToolError(f"cannot release agent lock: failed to remove owner.json for {agent_id}") from exc
    try:
        os.rmdir(lock_dir)
    except OSError as exc:
        raise ToolError(f"cannot release agent lock: failed to remove lock directory for {agent_id}") from exc


def default_paseo_runner(paseo_bin: str) -> Callable[[list[str]], tuple[int, str, str]]:
    def run(args: list[str]) -> tuple[int, str, str]:
        cmd = [paseo_bin, *args]
        try:
            proc = subprocess.run(
                cmd,
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
    return data

def cli_json(runner: Callable[[list[str]], tuple[int, str, str]], args: list[str], label: str) -> Any:
    code, out, err = runner([*args, "--json"] if "--json" not in args else args)
    if code != 0:
        raise ToolError(f"{label}: exit {code}: {err or out}")
    return parse_cli_json(out, label)

def collect_paseo_census(
    runner: Callable[[list[str]], tuple[int, str, str]],
) -> dict[str, Any]:
    """Read-only census via Paseo CLI. Fail closed on missing/unparseable/page-capped."""
    # Unarchived agents (global). List items: id, status, ...
    unarchived = cli_json(runner, ["ls", "-g"], "paseo ls -g")
    if not isinstance(unarchived, list):
        raise ToolError("paseo ls -g: expected JSON array")
    unarchived_ids: set[str] = set()
    for item in unarchived:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ToolError("paseo ls -g: malformed agent entry")
        if not is_uuid(item["id"]):
            raise ToolError(f"paseo ls -g: non-uuid agent id {item['id']!r}")
        unarchived_ids.add(item["id"])

    schedules = cli_json(runner, ["schedule", "ls"], "paseo schedule ls")
    if not isinstance(schedules, list):
        raise ToolError("paseo schedule ls: expected JSON array")

    permits = cli_json(runner, ["permit", "ls"], "paseo permit ls")
    if not isinstance(permits, list):
        raise ToolError("paseo permit ls: expected JSON array")

    terminals = cli_json(runner, ["terminal", "ls", "--all"], "paseo terminal ls --all")
    if not isinstance(terminals, list):
        raise ToolError("paseo terminal ls: expected JSON array")

    return {
        "unarchived_ids": unarchived_ids,
        "unarchived": unarchived,
        "schedules": schedules,
        "permits": permits,
        "terminals": terminals,
    }

def inspect_agent(
    runner: Callable[[list[str]], tuple[int, str, str]], agent_id: str
) -> dict[str, Any]:
    data = cli_json(runner, ["inspect", agent_id], f"paseo inspect {agent_id}")
    if not isinstance(data, dict):
        raise ToolError(f"paseo inspect {agent_id}: expected object")
    return data

def agent_is_archived_or_closed(info: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
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
    reasons.append("agent_not_archived_or_closed")
    return False, reasons

def has_unarchived_descendant(
    runner: Callable[[list[str]], tuple[int, str, str]],
    agent_id: str,
    unarchived_ids: set[str],
) -> bool:
    """BFS via label filter for unarchived descendants."""
    if not unarchived_ids:
        return False
    frontier = [agent_id]
    seen = {agent_id}
    while frontier:
        parent = frontier.pop()
        label = f"{PARENT_LABEL}={parent}"
        children = cli_json(
            runner,
            ["ls", "-g", "--label", label],
            f"paseo ls descendants of {parent}",
        )
        if not isinstance(children, list):
            raise ToolError("paseo ls --label: expected JSON array")
        for item in children:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ToolError("paseo ls --label: malformed entry")
            cid = item["id"]
            if cid in seen:
                continue
            seen.add(cid)
            if cid in unarchived_ids:
                return True
            frontier.append(cid)
    return False

def schedule_targets_agent(schedule: dict[str, Any], agent_id: str) -> bool:
    if schedule.get("status") != "active":
        return False
    target = schedule.get("target")
    # Structured target (tests / richer runners)
    if isinstance(target, dict):
        ttype = target.get("type")
        tid = target.get("agentId")
        if ttype in ("agent", "self") and tid == agent_id:
            return True
        return False
    if not isinstance(target, str):
        return False
    short = agent_id[:7]
    # CLI formatTarget: agent:<7> / self:<7>
    if target in (f"agent:{short}", f"self:{short}", f"agent:{agent_id}", f"self:{agent_id}"):
        return True
    if target.endswith(agent_id) and (target.startswith("agent:") or target.startswith("self:")):
        return True
    return False

def permit_for_agent(permit: dict[str, Any], agent_id: str) -> bool:
    aid = permit.get("agentId") or permit.get("agent_id")
    return isinstance(aid, str) and aid == agent_id

def terminal_in_scratch(terminal: dict[str, Any], scratch_dir: str) -> bool:
    cwd = terminal.get("cwd")
    if not isinstance(cwd, str) or not cwd or cwd == "-":
        return False
    # Resolve only if absolute; do not follow symlinks for classification.
    if not os.path.isabs(cwd):
        return False
    return under(norm(cwd), scratch_dir)


def read_boot_id(proc_root: str) -> str:
    path = os.path.join(proc_root, "sys", "kernel", "random", "boot_id")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as exc:
        raise ToolError(f"unable to read boot_id: {exc}") from exc

def parse_stat_start(stat_text: str) -> int:
    lparen = stat_text.find("(")
    rparen = stat_text.rfind(")")
    if lparen < 0 or rparen < 0 or rparen <= lparen:
        raise ValueError("malformed stat")
    rest = stat_text[rparen + 2 :].split()
    # field 22 is starttime → index 19 after comm
    return int(rest[19])

def live_pid_map(proc_root: str) -> dict[int, int | None]:
    """pid -> start_time_ticks (None if unreadable but dir exists)."""
    out: dict[int, int | None] = {}
    try:
        names = os.listdir(proc_root)
    except OSError as exc:
        raise ToolError(f"unable to list {proc_root}: {exc}") from exc
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        pdir = os.path.join(proc_root, name)
        if not os.path.isdir(pdir):
            continue
        # kernel threads: empty cmdline often; still need starttime when readable
        try:
            with open(os.path.join(pdir, "stat"), encoding="utf-8") as f:
                out[pid] = parse_stat_start(f.read())
        except (OSError, ValueError, IndexError):
            if os.path.isdir(pdir):
                out[pid] = None
    return out

def load_snapshot(path: str) -> dict[str, Any] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None

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

def wait_for_snapshot(
    census_path: str,
    *,
    proc_root: str,
    started_at: datetime,
    now_fn: Callable[[], datetime],
    wait_s: float,
    poll_s: float,
) -> tuple[dict[str, Any] | None, list[str]]:
    boot_id = read_boot_id(proc_root)
    deadline = time.monotonic() + wait_s
    last_reasons = ["snapshot_missing"]
    while True:
        now = now_fn()
        snap = load_snapshot(census_path)
        if snap is not None:
            ok, reasons = snapshot_acceptable(
                snap, boot_id=boot_id, started_at=started_at, now=now
            )
            if ok:
                return snap, []
            last_reasons = reasons
        else:
            last_reasons = ["snapshot_missing"]
        if time.monotonic() >= deadline:
            return None, last_reasons
        time.sleep(poll_s)

def process_proof(
    snap: dict[str, Any],
    proc_root: str,
) -> tuple[bool, list[str], dict[int, dict[str, Any]]]:
    """Match every live pid against snapshot pid+start_time_ticks. Fail closed."""
    reasons: list[str] = []
    by_pid: dict[int, dict[str, Any]] = {}
    for rec in snap.get("processes") or []:
        if not isinstance(rec, dict):
            reasons.append("snapshot_process_malformed")
            continue
        pid = rec.get("pid")
        st = rec.get("start_time_ticks")
        if not isinstance(pid, int) or not isinstance(st, int):
            # error-class records may still have both; missing blocks
            if "error" in rec and isinstance(pid, int) and isinstance(st, int):
                by_pid[pid] = rec
                continue
            reasons.append("snapshot_process_fields")
            continue
        by_pid[pid] = rec

    live = live_pid_map(proc_root)
    for pid, live_st in live.items():
        if live_st is None:
            reasons.append("live_process_unreadable")
            continue
        if pid not in by_pid:
            reasons.append("process_new")
            continue
        snap_st = by_pid[pid].get("start_time_ticks")
        if snap_st != live_st:
            reasons.append("process_pid_mismatch")
    # de-dupe reasons preserve order
    seen: set[str] = set()
    uniq: list[str] = []
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
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            path = ref.get("path")
            if isinstance(path, str) and under(path, scratch_dir):
                hits.append(pid)
                break
    return hits


def make_token(
    agent_id: str,
    generation: str,
    released_at: str,
    size_bytes: int,
    st: os.stat_result,
) -> str:
    payload = "|".join(
        [
            agent_id,
            generation,
            released_at,
            str(size_bytes),
            str(st.st_dev),
            str(st.st_ino),
            str(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def list_scratch_candidates(runtime_root: str) -> list[str]:
    scratch_root = os.path.join(runtime_root, "scratch")
    if not os.path.isdir(scratch_root):
        return []
    ids: list[str] = []
    try:
        for name in sorted(os.listdir(scratch_root)):
            if is_uuid(name):
                ids.append(name)
    except OSError as exc:
        raise ToolError(f"cannot list scratch root: {exc}") from exc
    return ids

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
    }
    reasons: list[str] = []

    if not process_ok:
        reasons.extend(process_reasons or ["process_census_blocked"])
        out["reasons"] = reasons
        out["classification"] = "blocked"
        return out

    hint, man_reasons, manifest = classify_manifest(scratch_dir, agent_id, now)
    reasons.extend(man_reasons)

    size, size_err = dir_size_capped(scratch_dir, size_deadline)
    if size_err:
        reasons.append(size_err)
        out["reasons"] = reasons
        out["classification"] = "blocked"
        return out
    out["size_bytes"] = size

    # Skip foreign-lock protection only when this invocation already holds the lock
    # for final archive revalidation (self-held mkdir lock is expected).
    if holding_lock_for != agent_id:
        held, lock_reason = lock_present(runtime_root, agent_id)
        if held:
            reasons.append(lock_reason or "lock_present")
            out["reasons"] = reasons
            out["classification"] = "protected"
            return out

    # Paseo agent identity
    try:
        info = inspect_agent(runner, agent_id)
    except ToolError as exc:
        msg = str(exc).lower()
        if "not found" in msg or "exit" in msg:
            reasons.append("agent_unknown")
        else:
            reasons.append("agent_inspect_failed")
        out["reasons"] = reasons
        out["classification"] = "blocked"
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

    try:
        if has_unarchived_descendant(runner, agent_id, paseo["unarchived_ids"]):
            reasons.append("unarchived_descendant")
            out["reasons"] = reasons
            out["classification"] = "protected"
            return out
    except ToolError:
        reasons.append("descendant_census_failed")
        out["reasons"] = reasons
        out["classification"] = "blocked"
        return out

    for sch in paseo["schedules"]:
        if isinstance(sch, dict) and schedule_targets_agent(sch, agent_id):
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

    for term in paseo["terminals"]:
        if isinstance(term, dict) and terminal_in_scratch(term, scratch_dir):
            reasons.append("terminal_in_scratch")
            out["reasons"] = reasons
            out["classification"] = "protected"
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

    try:
        st = os.lstat(scratch_dir)
    except OSError:
        reasons.append("scratch_stat_failed")
        out["reasons"] = reasons
        out["classification"] = "blocked"
        return out

    gen = str(manifest["generation"])
    released_at = str(manifest["releasedAt"])
    out["generation"] = gen
    out["released_at"] = released_at
    out["candidate_token"] = make_token(agent_id, gen, released_at, int(size or 0), st)
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
    size_deadline = time.monotonic() + SIZE_WALK_TIMEOUT_S

    free = {
        "root": free_bytes("/"),
        "runtime_root": free_bytes(runtime_root),
    }
    report = {
        "artifacts_bytes": report_aggregate(runtime_root, "artifacts", size_deadline),
        "quarantine_bytes": report_aggregate(runtime_root, "quarantine", size_deadline),
    }

    # Process snapshot (post-start, complete, ≤45s)
    snap, snap_reasons = wait_for_snapshot(
        census_path,
        proc_root=proc_root,
        started_at=started,
        now_fn=now_fn,
        wait_s=wait_s,
        poll_s=poll_s,
    )
    process_ok = False
    process_reasons = list(snap_reasons)
    by_pid: dict[int, dict[str, Any]] = {}
    if snap is not None:
        process_ok, process_reasons, by_pid = process_proof(snap, proc_root)

    # Paseo census — if it fails, block all candidates
    paseo_ok = True
    paseo_reasons: list[str] = []
    paseo: dict[str, Any] = {
        "unarchived_ids": set(),
        "unarchived": [],
        "schedules": [],
        "permits": [],
        "terminals": [],
    }
    try:
        paseo = collect_paseo_census(runner)
    except ToolError as exc:
        paseo_ok = False
        paseo_reasons.append(f"paseo_census:{exc}")

    if not paseo_ok:
        process_ok = False
        process_reasons = process_reasons + paseo_reasons

    now = now_fn()
    candidates = []
    for agent_id in list_scratch_candidates(runtime_root):
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
            )
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_iso,
        "runtime_root": runtime_root,
        "free_bytes": free,
        "report_only": report,
        "process_census": {
            "status": "ok" if process_ok else "blocked",
            "reasons": process_reasons,
            "captured_at": None if snap is None else snap.get("captured_at"),
            "boot_id": None if snap is None else snap.get("boot_id"),
        },
        "candidates": candidates,
    }


def inventory_tree(root: str) -> tuple[list[tuple[str, os.stat_result]], list[str]]:
    """Inventory files then dirs (deepest first). Never follows symlinks."""
    errors: list[str] = []
    files: list[tuple[str, os.stat_result]] = []
    dirs: list[tuple[str, os.stat_result]] = []

    try:
        root_st = os.lstat(root)
    except OSError:
        return [], ["root_missing"]
    if stat.S_ISLNK(root_st.st_mode):
        return [], ["root_is_symlink"]
    if not stat.S_ISDIR(root_st.st_mode):
        return [], ["root_not_directory"]
    root_dev = root_st.st_dev

    def walk(path: str) -> None:
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
            files.append((path, st))
            return
        if stat.S_ISDIR(st.st_mode):
            dirs.append((path, st))
            try:
                with os.scandir(path) as it:
                    for ent in it:
                        walk(ent.path)
            except OSError:
                errors.append("unreadable_dir")
            return
        errors.append("special_file")

    walk(root)
    dirs.sort(key=lambda x: x[0].count(os.sep), reverse=True)
    return files + dirs, errors

def remove_scratch_exact(scratch_dir: str) -> int:
    """Remove only the exact scratch directory. Symlink-safe, fail-closed."""
    entries, errors = inventory_tree(scratch_dir)
    if errors:
        raise ToolError(f"refuse scratch removal: {','.join(sorted(set(errors)))}")

    files = [(p, st) for p, st in entries if stat.S_ISREG(st.st_mode)]
    dirs = [(p, st) for p, st in entries if stat.S_ISDIR(st.st_mode)]
    dirs.sort(key=lambda x: x[0].count(os.sep), reverse=True)

    bytes_removed = 0
    for path, st0 in files:
        try:
            st1 = os.lstat(path)
        except OSError as exc:
            raise ToolError(f"path changed during verification: {path}") from exc
        if (
            st1.st_ino != st0.st_ino
            or st1.st_dev != st0.st_dev
            or st1.st_size != st0.st_size
            or stat.S_IFMT(st1.st_mode) != stat.S_IFMT(st0.st_mode)
            or st1.st_nlink > 1
        ):
            raise ToolError(f"contents changed during verification: {path}")
        bytes_removed += st1.st_size
        os.unlink(path)

    for path, st0 in dirs:
        try:
            st1 = os.lstat(path)
        except OSError as exc:
            raise ToolError(f"path changed during verification: {path}") from exc
        if st1.st_ino != st0.st_ino or st1.st_dev != st0.st_dev:
            raise ToolError(f"directory identity changed: {path}")
        try:
            remaining = os.listdir(path)
        except OSError as exc:
            raise ToolError(f"cannot list before rmdir: {path}") from exc
        if remaining:
            raise ToolError(f"directory not empty before rmdir: {path}")
        os.rmdir(path)

    if os.path.lexists(scratch_dir):
        raise ToolError(f"scratch directory still exists after removal: {scratch_dir}")
    return bytes_removed


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

    # Snapshot artifacts for byte-for-byte check
    art_before: dict[str, bytes] = {}
    if os.path.isdir(artifacts_dir) and not os.path.islink(artifacts_dir):
        for dirpath, _dirnames, filenames in os.walk(artifacts_dir):
            for name in filenames:
                p = os.path.join(dirpath, name)
                if os.path.islink(p):
                    continue
                try:
                    with open(p, "rb") as f:
                        art_before[p] = f.read()
                except OSError:
                    pass

    free_before = {
        "root": free_bytes("/"),
        "runtime_root": free_bytes(runtime_root),
    }

    lock_dir, lock_token = acquire_lock(runtime_root, agent_id)
    try:
        # Post-lock revalidation with a new started_at so snapshot must be fresh after lock.
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

        bytes_removed = remove_scratch_exact(scratch_dir)

        # Artifacts must be untouched
        for p, content in art_before.items():
            try:
                with open(p, "rb") as f:
                    if f.read() != content:
                        raise ToolError(f"artifacts mutated during archive: {p}")
            except OSError as exc:
                raise ToolError(f"artifacts missing after archive: {p}") from exc

        free_after = {
            "root": free_bytes("/"),
            "runtime_root": free_bytes(runtime_root),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "archived",
            "agent_id": agent_id,
            "generation": generation,
            "path": scratch_dir,
            "bytes_removed": bytes_removed,
            "free_bytes_before": free_before,
            "free_bytes_after": free_after,
            "artifacts_preserved": True,
        }
    finally:
        release_lock(lock_dir, agent_id, lock_token)


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
    p.add_argument(
        "--proc-root",
        default="/proc",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--wait-seconds",
        type=float,
        default=SNAPSHOT_WAIT_S,
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--poll-seconds",
        type=float,
        default=0.25,
        help=argparse.SUPPRESS,
    )
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
