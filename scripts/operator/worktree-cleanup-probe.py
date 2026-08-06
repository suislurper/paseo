#!/usr/bin/env python3
"""Read-only, fail-closed local evidence for Paseo worktree cleanup.

Paseo remains authoritative for the managed-worktree list and archive action.
This probe only makes the local safety checks deterministic.  It never changes
Git, files, processes, services, schedules, agents, or Paseo state.

Process ownership uses the root-owned process-census snapshot (same consumer
merge rules as agent-scratch-cleanup.py), not an unprivileged inline /proc path
scan of foreign command lines.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Shared process-census consumer helpers (sibling load — do not copy parser)
# ---------------------------------------------------------------------------

_ASC_PATH = Path(__file__).resolve().with_name("agent-scratch-cleanup.py")
_spec = importlib.util.spec_from_file_location("_paseo_agent_scratch_cleanup", _ASC_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"unable to load shared operator helpers from {_ASC_PATH}")
_asc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_asc)

ToolError = _asc.ToolError
wait_for_snapshot = _asc.wait_for_snapshot
process_proof = _asc.process_proof
ensure_no_symlink_components = _asc.ensure_no_symlink_components
DEFAULT_CENSUS = _asc.DEFAULT_CENSUS
SNAPSHOT_WAIT_S = _asc.SNAPSHOT_WAIT_S
REF_KINDS = _asc.REF_KINDS

VERSION = 1
DEFAULT_REPO = "/mnt/data/shab"
DEFAULT_MANAGED_ROOT = "/home/user/.paseo/worktrees"
DEFAULT_POLICY = "/home/user/.paseo/WORKTREE_CLEANUP_POLICY.md"
ALLOWED_IGNORED_COMPONENTS = {
    "node_modules",
    ".next",
    ".turbo",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".import_linter_cache",
    "__pycache__",
    "dist",
}
PROTECTED_WORDS = (
    "data",
    "runs",
    "artifacts",
    "results",
    "receipts",
    "evidence",
    "logs",
    "snapshots",
    "backups",
    ".done",
    "database",
    "model_output",
    "model-output",
    "corpus",
    "corpora",
)
DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".mdb", ".duckdb")


class ProbeError(RuntimeError):
    pass


def run(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout: float = 30,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"timeout:{argv[0]}:{timeout:g}s") from exc
    except OSError as exc:
        raise ProbeError(f"launch:{argv[0]}:{exc}") from exc


def checked(argv: list[str], *, cwd: str | None = None, timeout: float = 30) -> str:
    result = run(argv, cwd=cwd, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise ProbeError(f"command:{argv[0]}:exit={result.returncode}:{detail}")
    return result.stdout


def json_command(argv: list[str], *, timeout: float = 30) -> Any:
    raw = checked(argv, timeout=timeout)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"json:{argv[0]}:{exc}") from exc


def field(mapping: Any, *names: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def normalize_path(value: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(value)))


def is_within(path: str, root: str) -> bool:
    pairs = (
        (normalize_path(path), normalize_path(root)),
        (os.path.realpath(normalize_path(path)), os.path.realpath(normalize_path(root))),
    )
    for candidate, boundary in pairs:
        try:
            if os.path.commonpath((candidate, boundary)) == boundary:
                return True
        except ValueError:
            pass
    return False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso(dt: datetime | None = None) -> str:
    value = dt or utc_now()
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_manual_pins(policy_path: str) -> list[str]:
    try:
        text = Path(policy_path).read_text()
    except OSError as exc:
        raise ProbeError(f"manual-pins:policy-read:{exc}") from exc
    match = re.search(r"^## Manual pins\s*$([\s\S]*?)(?=^## |\Z)", text, re.MULTILINE)
    if not match:
        raise ProbeError("manual-pins:section-missing")
    pins = re.findall(
        r"(?m)^- `([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})`\s*$",
        match.group(1),
    )
    if not pins:
        raise ProbeError("manual-pins:none")
    if len(pins) != len(set(pins)):
        raise ProbeError("manual-pins:duplicate")
    return pins


def decode_git_path(raw: bytes) -> str:
    return os.fsdecode(raw)


def parse_porcelain_v1_z(raw: bytes) -> list[dict[str, str]]:
    """Parse `git status --porcelain=v1 -z`, including rename pairs."""
    if not raw:
        return []
    records = raw.split(b"\0")
    if records[-1] != b"":
        raise ProbeError("porcelain:missing-nul-terminator")
    records.pop()
    parsed: list[dict[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2:3] != b" ":
            raise ProbeError(f"porcelain:malformed-record:{record[:80]!r}")
        status = record[:2].decode("ascii", "strict")
        path = decode_git_path(record[3:])
        if not path:
            raise ProbeError("porcelain:empty-path")
        entry = {"status": status, "path": path}
        if status not in {"!!", "??"} and (status[0] in "RC" or status[1] in "RC"):
            index += 1
            if index >= len(records) or not records[index]:
                raise ProbeError("porcelain:missing-rename-source")
            entry["source_path"] = decode_git_path(records[index])
        parsed.append(entry)
        index += 1
    return parsed


def protected_ignored_reason(path: str) -> str | None:
    normalized = path.rstrip("/")
    components = [part.lower() for part in PurePosixPath(normalized).parts if part not in {"", "."}]
    for component in components:
        if component.endswith(DATABASE_SUFFIXES):
            return "database"
        if any(word in component for word in PROTECTED_WORDS):
            return f"protected-name:{component}"
    return None


def env_is_rebuildable(candidate: str, canonical: str, relative: str) -> tuple[bool, str]:
    candidate_file = Path(candidate, relative)
    canonical_file = Path(canonical, relative)
    try:
        if candidate_file.is_symlink() and candidate_file.resolve(strict=True) == canonical_file.resolve(strict=True):
            return True, "symlink-to-canonical"
        if not candidate_file.is_file() or not canonical_file.is_file():
            return False, "env-counterpart-missing"
        if candidate_file.read_bytes() == canonical_file.read_bytes():
            return True, "byte-identical-to-canonical"
        return False, "env-differs-from-canonical"
    except OSError as exc:
        return False, f"env-compare-error:{exc}"


def classify_ignored(entries: list[dict[str, str]], candidate: str, canonical: str) -> tuple[list[dict[str, str]], list[str]]:
    evidence: list[dict[str, str]] = []
    blockers: list[str] = []
    for entry in entries:
        if entry["status"] != "!!":
            continue
        path = entry["path"].rstrip("/")
        reason = protected_ignored_reason(path)
        if reason:
            evidence.append({"path": path, "class": "protected", "reason": reason})
            blockers.append(f"ignored:{path}:{reason}")
            continue
        name = PurePosixPath(path).name.lower()
        if name in {".env", ".env.local"}:
            allowed, env_reason = env_is_rebuildable(candidate, canonical, path)
            evidence.append({"path": path, "class": "allowed" if allowed else "blocked", "reason": env_reason})
            if not allowed:
                blockers.append(f"ignored:{path}:{env_reason}")
            continue
        components = {part.lower() for part in PurePosixPath(path).parts}
        if components & ALLOWED_IGNORED_COMPONENTS or path.lower().endswith((".pyc", ".pyo")):
            evidence.append({"path": path, "class": "allowed", "reason": "rebuildable"})
        else:
            evidence.append({"path": path, "class": "blocked", "reason": "not-allowlisted"})
            blockers.append(f"ignored:{path}:not-allowlisted")
    return evidence, blockers


def parse_worktree_porcelain(raw: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in raw.splitlines() + [""]:
        if not line:
            if current:
                items.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key in {"bare", "detached", "locked", "prunable"}:
            current[key] = value or True
        else:
            current[key] = value
    return items


def resolve_remote_default(repo: str) -> dict[str, str]:
    raw = checked(["git", "-C", repo, "ls-remote", "--symref", "origin", "HEAD"], timeout=45)
    ref: str | None = None
    sha: str | None = None
    for line in raw.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            ref = line[5:].split("\t", 1)[0]
        elif line.endswith("\tHEAD") and re.fullmatch(r"[0-9a-fA-F]{40}\tHEAD", line):
            sha = line.split("\t", 1)[0].lower()
    if not ref or not sha:
        raise ProbeError("remote-default:incomplete-ls-remote")
    return {"remote": "origin", "ref": ref, "sha": sha}


def process_owners_from_snapshot(
    path: str,
    git_dir: str | None,
    by_pid: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert redacted census references into owner evidence (no argv/env).

    Protect when any cwd / exe / interpreter_script / open_fd path falls under
    the checkout or its exact git-dir.
    """
    owners: list[dict[str, Any]] = []
    roots = [path]
    if isinstance(git_dir, str) and git_dir:
        roots.append(git_dir)
    for pid, rec in sorted(by_pid.items(), key=lambda item: item[0]):
        refs = rec.get("references") or []
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            kind = ref.get("kind")
            ref_path = ref.get("path")
            if kind not in REF_KINDS or not isinstance(ref_path, str):
                continue
            if any(is_within(ref_path, root) for root in roots):
                owners.append(
                    {
                        "pid": pid,
                        "uid": rec.get("uid"),
                        "name": rec.get("name"),
                        "kind": kind,
                        "path": ref_path,
                    }
                )
    return owners


def collect_process_census(
    *,
    census_path: str,
    proc_root: str,
    managed_root: str,
    started_at: datetime,
    now_fn: Callable[[], datetime],
    wait_s: float,
    poll_s: float,
) -> tuple[bool, list[str], dict[int, dict[str, Any]], dict[str, Any]]:
    """Wait for a post-start ≤45s complete same-boot snapshot and prove identity.

    Returns (ok, reasons, by_pid, summary). Failures block all candidates.
    """
    summary: dict[str, Any] = {
        "complete": False,
        "status": "blocked",
        "reasons": [],
        "captured_at": None,
        "boot_id": None,
    }
    try:
        ensure_no_symlink_components(census_path, "process census path")
        if os.path.islink(census_path):
            raise ToolError(f"process census path is a symlink: {census_path}")
        snap, snap_reasons = wait_for_snapshot(
            census_path,
            proc_root=proc_root,
            started_at=started_at,
            now_fn=now_fn,
            wait_s=wait_s,
            poll_s=poll_s,
        )
    except ToolError as exc:
        summary["reasons"] = [f"snapshot_wait:{exc}"]
        return False, list(summary["reasons"]), {}, summary

    if snap is None:
        summary["reasons"] = list(snap_reasons) or ["snapshot_missing"]
        return False, list(summary["reasons"]), {}, summary

    summary["captured_at"] = snap.get("captured_at")
    summary["boot_id"] = snap.get("boot_id")
    try:
        process_ok, process_reasons, by_pid = process_proof(snap, proc_root, managed_root)
    except ToolError as exc:
        summary["reasons"] = [f"process_proof:{exc}"]
        return False, list(summary["reasons"]), {}, summary

    summary["complete"] = process_ok
    summary["status"] = "ok" if process_ok else "blocked"
    summary["reasons"] = list(process_reasons)
    return process_ok, list(process_reasons), by_pid, summary


def paseo_census(manual_pins: list[str]) -> dict[str, Any]:
    errors: list[str] = []
    agents: list[dict[str, Any]] = []
    try:
        raw_agents = json_command(["paseo", "ls", "--global", "--json"], timeout=45)
        if not isinstance(raw_agents, list):
            raise ProbeError("agents:not-array")
        if len(raw_agents) >= 200:
            raise ProbeError("agents:page-cap-reached")
        for item in raw_agents:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ProbeError("agents:invalid-record")
            if not isinstance(item.get("cwd"), str):
                detail = json_command(["paseo", "inspect", item["id"], "--json"], timeout=30)
                cwd = field(detail, "cwd", "Cwd")
                if not isinstance(cwd, str):
                    raise ProbeError(f"agents:cwd-unresolved:{item['id']}")
                item = {**item, "cwd": cwd}
            agents.append({
                "id": item["id"],
                "status": item.get("status"),
                "cwd": normalize_path(item["cwd"]),
            })
    except ProbeError as exc:
        errors.append(str(exc))

    pins: list[dict[str, Any]] = []
    for agent_id in manual_pins:
        try:
            detail = json_command(["paseo", "inspect", agent_id, "--json"], timeout=30)
            cwd = field(detail, "cwd", "Cwd")
            pins.append({
                "agent_id": agent_id,
                "cwd": normalize_path(cwd) if isinstance(cwd, str) else None,
                "status": field(detail, "status", "Status"),
                "path_exists": bool(isinstance(cwd, str) and os.path.exists(normalize_path(cwd))),
            })
        except ProbeError as exc:
            errors.append(f"pin:{agent_id}:{exc}")

    schedules: list[dict[str, Any]] = []
    try:
        listed = json_command(["paseo", "schedule", "ls", "--json"], timeout=30)
        if not isinstance(listed, list):
            raise ProbeError("schedules:not-array")
        if len(listed) >= 200:
            raise ProbeError("schedules:page-cap-reached")
        for item in listed:
            if not isinstance(item, dict) or item.get("status") != "active":
                continue
            schedule_id = item.get("id")
            if not isinstance(schedule_id, str):
                raise ProbeError("schedules:missing-id")
            detail = json_command(
                ["paseo", "schedule", "inspect", schedule_id, "--identity-only", "--json"],
                timeout=30,
            )
            target = detail.get("target") if isinstance(detail, dict) else None
            target_id = target.get("agentId") if isinstance(target, dict) else None
            target_cwd = next(
                (agent["cwd"] for agent in agents if agent["id"] == target_id),
                None,
            )
            if isinstance(target_id, str) and target_cwd is None:
                target_detail = json_command(["paseo", "inspect", target_id, "--json"], timeout=30)
                raw_target_cwd = field(target_detail, "cwd", "Cwd")
                if not isinstance(raw_target_cwd, str):
                    raise ProbeError(f"schedules:target-cwd-unresolved:{schedule_id}:{target_id}")
                target_cwd = normalize_path(raw_target_cwd)
            if isinstance(detail, dict):
                detail = {**detail, "targetCwd": target_cwd}
            schedules.append(detail)
    except ProbeError as exc:
        errors.append(str(exc))

    terminals: Any = None
    permissions: Any = None
    try:
        terminals = json_command(["paseo", "terminal", "ls", "--all", "--json"], timeout=30)
        if not isinstance(terminals, list):
            raise ProbeError("terminals:not-array")
        if len(terminals) >= 200:
            raise ProbeError("terminals:page-cap-reached")
    except ProbeError as exc:
        errors.append(str(exc))
    try:
        permissions = json_command(["paseo", "permit", "ls", "--json"], timeout=30)
        if not isinstance(permissions, list):
            raise ProbeError("permissions:not-array")
        if len(permissions) >= 200:
            raise ProbeError("permissions:page-cap-reached")
    except ProbeError as exc:
        errors.append(str(exc))

    return {
        "complete": not errors,
        "errors": errors,
        "agents": agents,
        "pins": pins,
        "active_schedules": schedules,
        "terminals": terminals,
        "pending_permissions": permissions,
    }


def relevant_agent_ids(path: str, census: dict[str, Any]) -> list[str]:
    return sorted(agent["id"] for agent in census["agents"] if is_within(agent["cwd"], path))


def pinned_agent_ids(path: str, census: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for pin in census["pins"]:
        cwd = pin.get("cwd")
        if isinstance(cwd, str) and (is_within(cwd, path) or is_within(path, cwd)):
            result.append(pin["agent_id"])
    return sorted(result)


def scheduled_agent_ids(path: str, census: dict[str, Any]) -> list[str]:
    result: set[str] = set()
    for schedule in census["active_schedules"]:
        target = schedule.get("target") if isinstance(schedule, dict) else None
        target_cwd = schedule.get("targetCwd") if isinstance(schedule, dict) else None
        if (
            isinstance(target, dict)
            and target.get("type") == "agent"
            and isinstance(target.get("agentId"), str)
            and isinstance(target_cwd, str)
            and is_within(target_cwd, path)
        ):
            result.add(target["agentId"])
    return sorted(result)


def terminal_matches(path: str, terminals: Any) -> tuple[list[dict[str, Any]], int]:
    matches: list[dict[str, Any]] = []
    unresolved = 0
    for terminal in terminals or []:
        cwd = field(terminal, "cwd", "Cwd", "workingDirectory", "worktreePath")
        if isinstance(cwd, str):
            if is_within(cwd, path):
                matches.append(terminal)
        else:
            unresolved += 1
    return matches, unresolved


def permission_matches(
    path: str, permissions: Any, agents: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    matches: list[dict[str, Any]] = []
    unresolved = 0
    cwd_by_agent = {agent["id"]: agent["cwd"] for agent in agents}
    for permission in permissions or []:
        agent_id = field(permission, "agentId", "AgentId", "agent_id")
        cwd = cwd_by_agent.get(agent_id) if isinstance(agent_id, str) else None
        if cwd is None:
            unresolved += 1
        elif is_within(cwd, path):
            matches.append(permission)
    return matches, unresolved


def lock_census() -> tuple[list[dict[str, Any]], str | None]:
    result = run(["lslocks", "--json", "--output", "PID,COMMAND,PATH"], timeout=30)
    if result.returncode != 0:
        return [], f"lslocks:exit={result.returncode}:{result.stderr.strip()[:300]}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [], f"lslocks:json:{exc}"
    locks = payload.get("locks") if isinstance(payload, dict) else None
    if not isinstance(locks, list):
        return [], "lslocks:missing-locks-array"
    return [item for item in locks if isinstance(item, dict)], None


def candidate_locks(path: str, git_dir: str, locks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item in locks:
        lock_path = item.get("path")
        if isinstance(lock_path, str) and (is_within(lock_path, path) or is_within(lock_path, git_dir)):
            found.append(item)
    for name in ("index.lock", "HEAD.lock", "locked"):
        candidate = os.path.join(git_dir, name)
        if os.path.lexists(candidate):
            found.append({"pid": None, "command": "filesystem-marker", "path": candidate})
    return found


def size_bytes(path: str) -> tuple[int | None, str | None]:
    """Bounded checkout size. Timeout is exactly 60s (fail-closed)."""
    try:
        result = run(["du", "-sx", "--block-size=1", "--", path], timeout=60)
    except ProbeError as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, f"du:exit={result.returncode}:{result.stderr.strip()[:300]}"
    first = result.stdout.split(maxsplit=1)[0] if result.stdout else ""
    if not first.isdigit():
        return None, "du:invalid-output"
    return int(first), None


IGNORED_STATUS_ARGV = [
    "git",
    "status",
    "--porcelain=v1",
    "-z",
    "--ignored=matching",
    "-unormal",
]


def inspect_candidate(
    item: dict[str, Any],
    *,
    repo: str,
    managed_root: str,
    remote: dict[str, str],
    census: dict[str, Any],
    process_ok: bool,
    process_reasons: list[str],
    by_pid: dict[int, dict[str, Any]],
    locks: list[dict[str, Any]],
    lock_error: str | None,
) -> dict[str, Any]:
    path = normalize_path(item["worktree"])
    blockers: list[str] = []
    evidence: dict[str, Any] = {}
    if not is_within(path, managed_root) or path == normalize_path(managed_root):
        blockers.append("not-under-managed-root")
    try:
        resolved = str(Path(path).resolve(strict=True))
        if resolved != path:
            blockers.append(f"path-alias:{resolved}")
    except OSError as exc:
        blockers.append(f"path-unresolvable:{exc}")

    def git_value(args: list[str]) -> str | None:
        try:
            return checked(["git", "-C", path, *args], timeout=30).strip()
        except ProbeError as exc:
            blockers.append(str(exc))
            return None

    top = git_value(["rev-parse", "--show-toplevel"])
    git_dir = git_value(["rev-parse", "--absolute-git-dir"])
    common_dir = git_value(["rev-parse", "--path-format=absolute", "--git-common-dir"])
    branch = git_value(["symbolic-ref", "-q", "--short", "HEAD"])
    head = git_value(["rev-parse", "HEAD"])
    evidence.update({"top_level": top, "git_dir": git_dir, "common_dir": common_dir, "branch": branch, "head": head})
    if top != path:
        blockers.append("git-top-level-mismatch")
    if path == normalize_path(repo):
        blockers.append("main-checkout")
    if not git_dir or not common_dir or git_dir == common_dir or "worktrees" not in Path(git_dir).parts:
        blockers.append("not-linked-worktree")
    remote_default_branch = remote["ref"].removeprefix("refs/heads/")
    if not branch or branch == remote_default_branch:
        blockers.append("unnamed-or-default-branch")
    expected_head = item.get("HEAD")
    expected_branch = item.get("branch", "")
    expected_branch = expected_branch.removeprefix("refs/heads/") if isinstance(expected_branch, str) else None
    if expected_head and head != expected_head:
        blockers.append("head-changed-from-worktree-list")
    if expected_branch and branch != expected_branch:
        blockers.append("branch-changed-from-worktree-list")
    if item.get("locked"):
        blockers.append("git-worktree-locked")

    dirty: list[dict[str, str]] | None = None
    ignored: list[dict[str, str]] | None = None
    try:
        status_result = run(
            ["git", "-C", path, "status", "--porcelain=v1", "-z", "-uall"],
            timeout=45,
            text=False,
        )
        if status_result.returncode != 0:
            raise ProbeError(f"git-status:exit={status_result.returncode}:{os.fsdecode(status_result.stderr)[:300]}")
        dirty = parse_porcelain_v1_z(status_result.stdout)
        if dirty:
            blockers.append(f"dirty:{len(dirty)}")
    except ProbeError as exc:
        blockers.append(str(exc))
    try:
        # Exact ignored inventory command — do not weaken flags.
        ignored_result = run(
            ["git", "-C", path, *IGNORED_STATUS_ARGV[1:]],
            timeout=45,
            text=False,
        )
        if ignored_result.returncode != 0:
            raise ProbeError(f"git-ignored:exit={ignored_result.returncode}:{os.fsdecode(ignored_result.stderr)[:300]}")
        ignored_status = parse_porcelain_v1_z(ignored_result.stdout)
        ignored, ignored_blockers = classify_ignored(ignored_status, path, repo)
        blockers.extend(ignored_blockers)
    except (ProbeError, UnicodeError) as exc:
        blockers.append(f"ignored-unknown:{exc}")

    object_available = False
    ancestor = False
    ahead: int | None = None
    if head:
        object_check = run(["git", "-C", path, "cat-file", "-e", f"{remote['sha']}^{{commit}}"], timeout=30)
        object_available = object_check.returncode == 0
        if not object_available:
            blockers.append("remote-default-object-unavailable")
        else:
            ancestor_check = run(["git", "-C", path, "merge-base", "--is-ancestor", head, remote["sha"]], timeout=30)
            ancestor = ancestor_check.returncode == 0
            if not ancestor:
                blockers.append("head-not-ancestor-of-remote-default")
            try:
                ahead_text = checked(["git", "-C", path, "rev-list", "--count", f"{remote['sha']}..{head}"], timeout=30).strip()
                ahead = int(ahead_text)
                if ahead != 0:
                    blockers.append(f"ahead:{ahead}")
            except (ProbeError, ValueError) as exc:
                blockers.append(f"ahead-unknown:{exc}")

    owners = relevant_agent_ids(path, census)
    pins = pinned_agent_ids(path, census)
    scheduled = scheduled_agent_ids(path, census)
    if owners:
        blockers.append(f"unarchived-agents:{','.join(owners)}")
    if pins:
        blockers.append(f"manual-pins:{','.join(pins)}")
    if scheduled:
        blockers.append(f"active-schedules-for:{','.join(scheduled)}")
    terminals, unresolved_terminals = terminal_matches(path, census.get("terminals"))
    permissions, unresolved_permissions = permission_matches(
        path, census.get("pending_permissions"), census["agents"]
    )
    if terminals:
        blockers.append(f"terminals:{len(terminals)}")
    if unresolved_terminals:
        blockers.append(f"terminal-census-unresolved:{unresolved_terminals}")
    if permissions:
        blockers.append(f"pending-permissions:{len(permissions)}")
    if unresolved_permissions:
        blockers.append(f"permission-census-unresolved:{unresolved_permissions}")

    processes = process_owners_from_snapshot(path, git_dir, by_pid if process_ok else {})
    if processes:
        blockers.append(f"processes:{','.join(str(item['pid']) for item in processes)}")
    if not process_ok:
        reason_tag = ",".join(process_reasons) if process_reasons else "blocked"
        blockers.append(f"process-census-incomplete:{reason_tag}")

    candidate_lock_items = candidate_locks(path, git_dir or path, locks)
    if candidate_lock_items:
        blockers.append(f"locks:{len(candidate_lock_items)}")
    if lock_error:
        blockers.append(f"lock-census-incomplete:{lock_error}")

    measured_size, size_error = size_bytes(path)
    if size_error:
        blockers.append(f"size-unknown:{size_error}")

    token_payload = {
        "path": path,
        "head": head,
        "branch": branch,
        "dirty": dirty,
        "ignored": ignored,
        "owners": owners,
        "pins": pins,
        "scheduled": scheduled,
        "terminals": terminals,
        "permissions": permissions,
        "processes": processes,
        "locks": candidate_lock_items,
        "remote": remote,
        "blockers": sorted(set(blockers)),
    }
    token = hashlib.sha256(json.dumps(token_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "path": path,
        "local_gate": "pass" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "candidate_token": token,
        "size_bytes": measured_size,
        "size_error": size_error,
        "git": {
            **evidence,
            "dirty": dirty,
            "ignored": ignored,
            "remote_default_object_available": object_available,
            "head_ancestor_of_remote_default": ancestor,
            "ahead": ahead,
        },
        "owners": owners,
        "manual_pins": pins,
        "scheduled_owners": scheduled,
        "terminals": terminals,
        "pending_permissions": permissions,
        "processes": processes,
        "locks": candidate_lock_items,
    }


def disk_free(path: str) -> dict[str, Any]:
    usage = os.statvfs(path)
    return {
        "path": path,
        "free_bytes": usage.f_bavail * usage.f_frsize,
        "total_bytes": usage.f_blocks * usage.f_frsize,
    }


def run_probe(
    *,
    repo: str,
    managed_root: str,
    policy: str,
    path: str | None = None,
    census_path: str = DEFAULT_CENSUS,
    proc_root: str = "/proc",
    wait_s: float = SNAPSHOT_WAIT_S,
    poll_s: float = 0.25,
    now_fn: Callable[[], datetime] | None = None,
    started_at: datetime | None = None,
) -> dict[str, Any]:
    now_fn = now_fn or utc_now
    started = started_at or now_fn()
    repo_n = normalize_path(repo)
    managed_n = normalize_path(managed_root)
    output: dict[str, Any] = {
        "schema_version": VERSION,
        "generated_at": utc_now_iso(now_fn()),
        "started_at": utc_now_iso(started),
        "complete": False,
        "fatal_errors": [],
        "repo": repo_n,
        "managed_root": managed_n,
        "disks": [],
        "remote_default": None,
        "paseo": None,
        "process_census": {
            "complete": False,
            "status": "blocked",
            "reasons": [],
            "captured_at": None,
            "boot_id": None,
        },
        "worktrees": [],
    }
    try:
        output["disks"] = [disk_free(repo_n), disk_free(managed_n)]
        remote = resolve_remote_default(repo_n)
        output["remote_default"] = remote
        worktree_items = parse_worktree_porcelain(
            checked(["git", "-C", repo_n, "worktree", "list", "--porcelain"], timeout=45)
        )
        candidates = [
            item for item in worktree_items
            if isinstance(item.get("worktree"), str) and is_within(item["worktree"], managed_n)
        ]
        if path:
            requested = normalize_path(path)
            candidates = [item for item in candidates if normalize_path(item["worktree"]) == requested]
            if len(candidates) != 1:
                raise ProbeError(f"requested-path-not-one-linked-worktree:{requested}:{len(candidates)}")
        manual_pins = read_manual_pins(normalize_path(policy))
        census = paseo_census(manual_pins)
        output["paseo"] = census
        if not census["complete"]:
            raise ProbeError("paseo-census-incomplete")

        process_ok, process_reasons, by_pid, process_summary = collect_process_census(
            census_path=census_path,
            proc_root=proc_root,
            managed_root=managed_n,
            started_at=started,
            now_fn=now_fn,
            wait_s=wait_s,
            poll_s=poll_s,
        )
        output["process_census"] = process_summary

        locks, lock_error = lock_census()
        output["worktrees"] = [
            inspect_candidate(
                item,
                repo=repo_n,
                managed_root=managed_n,
                remote=remote,
                census=census,
                process_ok=process_ok,
                process_reasons=process_reasons,
                by_pid=by_pid,
                locks=locks,
                lock_error=lock_error,
            )
            for item in candidates
        ]
        output["complete"] = True
    except (ProbeError, OSError, ValueError, ToolError) as exc:
        output["fatal_errors"].append(str(exc))
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--managed-root", default=DEFAULT_MANAGED_ROOT)
    parser.add_argument("--policy", default=DEFAULT_POLICY)
    parser.add_argument("--path", help="Inspect one exact linked-worktree path")
    parser.add_argument(
        "--process-census",
        default=DEFAULT_CENSUS,
        help="Root-owned process census snapshot path (default: /run/paseo/process-census.json).",
    )
    parser.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=SNAPSHOT_WAIT_S,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=0.25,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # argparse already wrote usage; keep nonzero and do not rebuild inventory.
        code = exc.code
        return int(code) if isinstance(code, int) else 2

    try:
        output = run_probe(
            repo=args.repo,
            managed_root=args.managed_root,
            policy=args.policy,
            path=args.path,
            census_path=args.process_census,
            proc_root=args.proc_root,
            wait_s=args.wait_seconds,
            poll_s=args.poll_seconds,
        )
        json.dump(output, sys.stdout, indent=2 if args.pretty else None, sort_keys=True)
        sys.stdout.write("\n")
        return 0 if output["complete"] else 2
    except Exception as exc:  # noqa: BLE001 — last-resort concise nonzero error
        print(f"worktree-cleanup-probe: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
