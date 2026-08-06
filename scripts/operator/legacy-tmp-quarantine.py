#!/usr/bin/env python3
"""Fail-closed operator tool: probe + quarantine one legacy /tmp candidate.
Exact one-candidate CLI. No daemon, DB, queue, automatic generic /tmp cleanup,
or glob expansion. See docs/operator-fork.md (Legacy /tmp recovery lane).
Process/Paseo census primitives reuse the exact producer/consumer semantics from
agent-scratch-cleanup.py (loaded by path; that module is not modified).
"""

from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
_ASC_PATH = Path(__file__).resolve().with_name("agent-scratch-cleanup.py")
_spec = importlib.util.spec_from_file_location("_paseo_agent_scratch_cleanup", _ASC_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"unable to load shared operator helpers from {_ASC_PATH}")
_asc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_asc)
for _n in ('ToolError', 'is_uuid', 'require_abs', 'norm', 'under', 'free_bytes', 'ensure_no_symlink_components', 'fsync_dir', 'mkdir_exact', 'default_paseo_runner', 'cli_json', 'inspect_agent', '_require_agent_id', 'wait_for_snapshot', 'process_proof', 'file_sha256', 'SNAPSHOT_WAIT_S', 'INVENTORY_TIMEOUT_S', 'DEFAULT_CENSUS'):
    globals()[_n] = getattr(_asc, _n)

SCHEMA_VERSION = 1
DEFAULT_TMP_ROOT = "/tmp"
DEFAULT_QUARANTINE_ROOT = "/mnt/data/paseo-runtime/quarantine/legacy-tmp"
DEFAULT_DATA_ROOT = "/mnt/data"
PRODUCER_PREFIXES = ("shab-stage-runner-","warm-live-api-","grok-run-","ask-expert-","expert-mcp-","cxc-","paseo-")
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def mtime_ns_of(st: os.stat_result) -> int:
    return int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
def producer_for(name: str) -> str | None:
    for prefix in PRODUCER_PREFIXES:
        if name.startswith(prefix):
            return prefix
    return None
def is_direct_child(path: str, parent: str) -> bool:
    p, r = norm(path), norm(parent)
    base = os.path.basename(p)
    return os.path.dirname(p) == r and bool(base) and base not in (".", "..") and os.sep not in base
def agent_cwd(info: dict[str, Any], label: str) -> str:
    cwd = info.get("Cwd") if isinstance(info.get("Cwd"), str) else info.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip() or cwd == "-":
        raise ToolError(f"{label}: missing or ambiguous agent cwd")
    cwd = cwd.strip()
    if not os.path.isabs(cwd):
        raise ToolError(f"{label}: non-absolute agent cwd")
    return norm(cwd)
def collect_paseo_protections(
    runner: Callable[[list[str]], tuple[int, str, str]], candidate: str
) -> list[str]:
    reasons: list[str] = []
    cand = norm(candidate)
    unarchived = cli_json(runner, ["ls", "-g"], "paseo ls -g")
    if not isinstance(unarchived, list):
        raise ToolError("paseo ls -g: expected JSON array")
    unarchived_ids: list[str] = []
    for item in unarchived:
        if not isinstance(item, dict):
            raise ToolError("paseo ls -g: malformed agent entry")
        aid = item.get("id") if isinstance(item.get("id"), str) else item.get("Id")
        unarchived_ids.append(_require_agent_id(aid, "paseo ls -g"))
    schedules = cli_json(runner, ["schedule", "ls"], "paseo schedule ls")
    if not isinstance(schedules, list):
        raise ToolError("paseo schedule ls: expected JSON array")
    for sch in schedules:
        if not isinstance(sch, dict):
            raise ToolError("paseo schedule ls: malformed entry")
        if not isinstance(sch.get("id"), str) or not sch["id"].strip():
            raise ToolError("paseo schedule ls: missing schedule id")
        if not isinstance(sch.get("status"), str) or not sch["status"]:
            raise ToolError("paseo schedule ls: missing schedule status")
    permits = cli_json(runner, ["permit", "ls"], "paseo permit ls")
    if not isinstance(permits, list):
        raise ToolError("paseo permit ls: expected JSON array")
    for perm in permits:
        if not isinstance(perm, dict):
            raise ToolError("paseo permit ls: malformed entry")
        _require_agent_id(perm.get("agentId") or perm.get("agent_id"), "paseo permit ls")
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
        if under(norm(cwd), cand):
            reasons.append("terminal_cwd_under_candidate")
    agent_cwds: dict[str, str] = {}
    for aid in unarchived_ids:
        agent_cwds[aid] = agent_cwd(inspect_agent(runner, aid), f"paseo inspect {aid}")
        if under(agent_cwds[aid], cand):
            reasons.append("agent_cwd_under_candidate")
    for sch in schedules:
        if sch.get("status") != "active":
            continue
        sid = str(sch["id"])
        identity = cli_json(
            runner,
            ["schedule", "inspect", sid, "--identity-only"],
            f"paseo schedule inspect {sid} --identity-only",
        )
        if not isinstance(identity, dict):
            raise ToolError(f"schedule identity {sid}: expected object")
        target = identity.get("target")
        if not isinstance(target, dict):
            raise ToolError(f"schedule identity {sid}: malformed target")
        ttype = target.get("type")
        if ttype == "new-agent":
            continue
        if ttype not in ("agent", "self"):
            raise ToolError(f"schedule identity {sid}: unknown target type {ttype!r}")
        tid = _require_agent_id(target.get("agentId") or target.get("agent_id"), f"schedule {sid}")
        if tid not in agent_cwds:
            agent_cwds[tid] = agent_cwd(inspect_agent(runner, tid), f"schedule target {tid}")
        if under(agent_cwds[tid], cand):
            reasons.append("active_schedule_target_cwd_under_candidate")
    for perm in permits:
        aid = _require_agent_id(perm.get("agentId") or perm.get("agent_id"), "permit")
        if aid not in agent_cwds:
            agent_cwds[aid] = agent_cwd(inspect_agent(runner, aid), f"permit agent {aid}")
        if under(agent_cwds[aid], cand):
            reasons.append("permitted_agent_cwd_under_candidate")
    seen: set[str] = set()
    out: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out
def processes_touching(by_pid: dict[int, dict[str, Any]], candidate: str) -> list[int]:
    hits = []
    for pid, rec in by_pid.items():
        for ref in rec.get("references") or []:
            path = ref.get("path") if isinstance(ref, dict) else None
            if isinstance(path, str) and under(path, candidate):
                hits.append(pid)
                break
    return hits
def wait_snapshot_safe(
    census_path: str,
    *,
    proc_root: str,
    started_at: datetime,
    now_fn: Callable[[], datetime],
    wait_s: float,
    poll_s: float,
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        ensure_no_symlink_components(census_path, "census path")
    except ToolError:
        return None, ["snapshot_path_is_symlink"]
    if os.path.islink(census_path):
        return None, ["snapshot_path_is_symlink"]
    return wait_for_snapshot(
        census_path,
        proc_root=proc_root,
        started_at=started_at,
        now_fn=now_fn,
        wait_s=wait_s,
        poll_s=poll_s,
    )
def symlink_inside(path: str, root_n: str) -> tuple[str | None, list[str]]:
    try:
        link_text = os.readlink(path)
    except OSError:
        return None, ["unreadable_symlink"]
    lexical = (
        norm(link_text)
        if os.path.isabs(link_text)
        else norm(os.path.join(os.path.dirname(path), link_text))
    )
    if not under(lexical, root_n):
        return None, ["symlink_escape"]
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return None, ["symlink_unresolvable"]
    if not under(norm(resolved), root_n):
        return None, ["symlink_escape"]
    return link_text, []
def candidate_inventory(root: str, deadline: float) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    try:
        root_st = os.lstat(root)
    except OSError:
        return [], ["root_missing"]
    if stat.S_ISLNK(root_st.st_mode):
        return [], ["root_is_symlink"]
    root_n, root_dev = norm(root), root_st.st_dev
    def rel_of(path: str) -> str:
        return "." if norm(path) == root_n else os.path.relpath(path, root_n)
    def base_rec(path: str, st: os.stat_result, typ: str) -> dict[str, Any]:
        return {
            "type": typ,
            "rel": rel_of(path),
            "mode": stat.S_IMODE(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "size": st.st_size if typ in ("file", "symlink") else 0,
            "mtime_ns": mtime_ns_of(st),
            "nlink": st.st_nlink,
            "dev": st.st_dev,
            "ino": st.st_ino,
        }
    def handle_file(path: str, st: os.stat_result) -> None:
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
        try:
            st2 = os.lstat(path)
        except OSError:
            errors.append("path_changed")
            return
        if (
            st2.st_ino != st.st_ino
            or st2.st_dev != st.st_dev
            or st2.st_size != st.st_size
            or mtime_ns_of(st2) != mtime_ns_of(st)
            or stat.S_IMODE(st2.st_mode) != stat.S_IMODE(st.st_mode)
        ):
            errors.append("path_changed")
            return
        rec = base_rec(path, st, "file")
        rec["sha256"] = digest
        records.append(rec)
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
            link_text, link_errs = symlink_inside(path, root_n)
            if link_errs:
                errors.extend(link_errs)
                return
            rec = base_rec(path, st, "symlink")
            rec["link_target"] = link_text
            records.append(rec)
            return
        if stat.S_ISREG(st.st_mode):
            handle_file(path, st)
            return
        if stat.S_ISDIR(st.st_mode):
            records.append(base_rec(path, st, "dir"))
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
    if stat.S_ISREG(root_st.st_mode):
        handle_file(root, root_st)
        return records, errors
    if not stat.S_ISDIR(root_st.st_mode):
        return [], ["special_file"]
    walk(root)
    records.sort(key=lambda r: (r["rel"], r["type"]))
    return records, errors
def inventory_fingerprint(records: list[dict[str, Any]]) -> str:
    slim = []
    for r in records:
        item = {
            "type": r["type"],
            "rel": r["rel"],
            "mode": r["mode"],
            "uid": r["uid"],
            "gid": r["gid"],
            "size": r["size"],
            "mtime_ns": r["mtime_ns"],
            "nlink": r["nlink"],
        }
        if r["type"] == "file":
            item["sha256"] = r.get("sha256")
        if r["type"] == "symlink":
            item["link_target"] = r.get("link_target")
        slim.append(item)
    return hashlib.sha256(
        json.dumps(slim, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
def make_token(source: str, producer: str, inv_fp: str, identity: dict[str, Any]) -> str:
    parts = [norm(source), producer, inv_fp]
    parts.extend(
        str(identity.get(k)) for k in ("type", "mode", "uid", "gid", "size", "mtime_ns", "nlink")
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
def size_from_inventory(records: list[dict[str, Any]]) -> int:
    return sum(int(r.get("size") or 0) for r in records if r["type"] == "file")
def identity_from_lstat(path: str) -> dict[str, Any]:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        typ = "symlink"
    elif stat.S_ISREG(st.st_mode):
        typ = "file"
    elif stat.S_ISDIR(st.st_mode):
        typ = "dir"
    else:
        typ = "special"
    return {
        "type": typ,
        "mode": stat.S_IMODE(st.st_mode),
        "uid": st.st_uid,
        "gid": st.st_gid,
        "size": st.st_size if typ in ("file", "symlink") else 0,
        "mtime_ns": mtime_ns_of(st),
        "nlink": st.st_nlink,
        "dev": st.st_dev,
        "ino": st.st_ino,
    }
def identities_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    keys = ("type", "mode", "uid", "gid", "size", "mtime_ns", "nlink", "dev", "ino")
    return all(a.get(k) == b.get(k) for k in keys)
def resolve_source(source: str, tmp_root: str) -> str:
    require_abs(source, "source")
    require_abs(tmp_root, "tmp root")
    ensure_no_symlink_components(tmp_root, "tmp root")
    if not is_direct_child(source, tmp_root):
        raise ToolError(f"source must be an absolute direct child of {tmp_root}")
    return norm(source)
def dual_inventory(source: str) -> tuple[list[dict[str, Any]], list[str]]:
    deadline = time.monotonic() + INVENTORY_TIMEOUT_S
    r1, e1 = candidate_inventory(source, deadline)
    if e1:
        return [], e1
    r2, e2 = candidate_inventory(source, deadline)
    if e2:
        return [], e2
    if inventory_fingerprint(r1) != inventory_fingerprint(r2):
        return [], ["tree_changed"]
    return r2, []
def classify_source(
    *,
    source: str,
    tmp_root: str,
    census_path: str,
    proc_root: str,
    runner: Callable[[list[str]], tuple[int, str, str]],
    now_fn: Callable[[], datetime],
    wait_s: float,
    poll_s: float,
    started_at: datetime | None = None,
    data_root: str = DEFAULT_DATA_ROOT,
) -> dict[str, Any]:
    started = started_at or now_fn()
    source = resolve_source(source, tmp_root)
    basename = os.path.basename(source)
    producer = producer_for(basename)
    free = {"root": free_bytes("/"), "mnt_data": free_bytes(data_root)}
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "started_at": started.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {
            "path": source,
            "basename": basename,
            "recognized_producer": producer,
            "type": None,
            "owner": None,
            "size_bytes": None,
        },
        "owner_evidence": None,
        "classification": "blocked",
        "reasons": [],
        "candidate_token": None,
        "inventory_fingerprint": None,
        "free_bytes": free,
        "process_census": {"status": "blocked", "reasons": [], "captured_at": None, "boot_id": None},
    }
    if producer is None:
        out["classification"] = "unknown"
        out["reasons"] = ["unknown_producer"]
        if os.path.lexists(source):
            try:
                ident = identity_from_lstat(source)
                out["source"]["type"] = ident["type"]
                out["source"]["owner"] = {"uid": ident["uid"], "gid": ident["gid"]}
                out["owner_evidence"] = {
                    "uid": ident["uid"],
                    "gid": ident["gid"],
                    "mode": ident["mode"],
                    "mtime_ns": ident["mtime_ns"],
                    "nlink": ident["nlink"],
                }
            except OSError:
                pass
        return out
    if not os.path.lexists(source):
        out["reasons"] = ["source_missing"]
        return out
    if os.path.islink(source):
        out["reasons"] = ["root_is_symlink"]
        return out
    try:
        ident = identity_from_lstat(source)
    except OSError:
        out["reasons"] = ["source_unreadable"]
        return out
    if ident["type"] not in ("file", "dir"):
        out["reasons"] = ["special_file"]
        return out
    out["source"]["type"] = ident["type"]
    out["source"]["owner"] = {"uid": ident["uid"], "gid": ident["gid"]}
    out["owner_evidence"] = {
        "uid": ident["uid"],
        "gid": ident["gid"],
        "mode": ident["mode"],
        "mtime_ns": ident["mtime_ns"],
        "nlink": ident["nlink"],
    }
    snap, snap_reasons = wait_snapshot_safe(
        census_path,
        proc_root=proc_root,
        started_at=started,
        now_fn=now_fn,
        wait_s=wait_s,
        poll_s=poll_s,
    )
    process_ok, process_reasons, by_pid = False, list(snap_reasons), {}
    if snap is not None:
        # Cover exact candidate via same roots rule as scratch (under-root check).
        process_ok, process_reasons, by_pid = process_proof(snap, proc_root, source)
        out["process_census"]["captured_at"] = snap.get("captured_at")
        out["process_census"]["boot_id"] = snap.get("boot_id")
    out["process_census"]["status"] = "ok" if process_ok else "blocked"
    out["process_census"]["reasons"] = process_reasons
    if not process_ok:
        out["reasons"] = process_reasons or ["process_census_blocked"]
        return out
    try:
        paseo_reasons = collect_paseo_protections(runner, source)
    except ToolError as exc:
        out["reasons"] = [f"paseo_census:{exc}"]
        return out
    if paseo_reasons:
        out["classification"] = "protected"
        out["reasons"] = paseo_reasons
        return out
    if processes_touching(by_pid, source):
        out["classification"] = "protected"
        out["reasons"] = ["process_reference_under_candidate"]
        return out
    records, inv_errors = dual_inventory(source)
    if inv_errors:
        out["reasons"] = sorted(set(inv_errors))
        return out
    try:
        if not identities_match(ident, identity_from_lstat(source)):
            out["reasons"] = ["path_changed"]
            return out
    except OSError:
        out["reasons"] = ["path_changed"]
        return out
    fp = inventory_fingerprint(records)
    out["source"]["size_bytes"] = size_from_inventory(records)
    out["inventory_fingerprint"] = fp
    out["candidate_token"] = make_token(source, producer, fp, ident)
    out["classification"] = "eligible"
    out["reasons"] = []
    out["_inventory"] = records
    out["_identity"] = ident
    return out
def run_probe(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in classify_source(**kwargs).items() if not k.startswith("_")}
def ensure_quarantine_root(qroot: str) -> None:
    require_abs(qroot, "quarantine root")
    ensure_no_symlink_components(qroot, "quarantine root")
    if os.path.lexists(qroot):
        if os.path.islink(qroot) or not os.path.isdir(qroot):
            raise ToolError(f"quarantine root is not a real directory: {qroot}")
    else:
        parent = os.path.dirname(qroot)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, mode=0o700, exist_ok=True)
        if not os.path.isdir(qroot):
            mkdir_exact(qroot, 0o700)
def _meta_owner(path: str, st: os.stat_result, *, follow: bool = True) -> None:
    try:
        if follow:
            os.chmod(path, stat.S_IMODE(st.st_mode))
    except OSError:
        pass
    try:
        if follow:
            os.chown(path, st.st_uid, st.st_gid)
        else:
            os.lchown(path, st.st_uid, st.st_gid)
    except OSError:
        pass
    try:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=follow)
    except (OSError, TypeError, AttributeError):
        if follow:
            try:
                os.utime(path, (st.st_atime, st.st_mtime))
            except OSError:
                pass
def copy_file_nofollow(src: str, dst: str, st: os.stat_result, deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ToolError("inventory_timeout")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    rfd = os.open(src, flags)
    try:
        with os.fdopen(rfd, "rb") as rf:
            rfd = -1
            wfd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(st.st_mode))
            try:
                with os.fdopen(wfd, "wb") as wf:
                    wfd = -1
                    while True:
                        if time.monotonic() > deadline:
                            raise ToolError("inventory_timeout")
                        chunk = rf.read(1024 * 1024)
                        if not chunk:
                            break
                        wf.write(chunk)
                    wf.flush()
                    os.fsync(wf.fileno())
            finally:
                if wfd >= 0:
                    os.close(wfd)
    finally:
        if rfd >= 0:
            os.close(rfd)
    _meta_owner(dst, st, follow=True)
def copy_candidate_tree(src: str, dst_payload: str, deadline: float) -> None:
    try:
        root_st = os.lstat(src)
    except OSError as exc:
        raise ToolError(f"cannot lstat source for copy: {exc}") from exc
    if stat.S_ISLNK(root_st.st_mode):
        raise ToolError("source became symlink before copy")
    if stat.S_ISREG(root_st.st_mode):
        copy_file_nofollow(src, dst_payload, root_st, deadline)
        return
    if not stat.S_ISDIR(root_st.st_mode):
        raise ToolError("source is not a regular file or directory")
    os.mkdir(dst_payload, stat.S_IMODE(root_st.st_mode))
    _meta_owner(dst_payload, root_st, follow=True)
    def walk(cur_src: str, cur_dst: str) -> None:
        if time.monotonic() > deadline:
            raise ToolError("inventory_timeout")
        try:
            with os.scandir(cur_src) as it:
                names = sorted(ent.name for ent in it)
        except OSError as exc:
            raise ToolError(f"unreadable_dir during copy: {cur_src}") from exc
        for name in names:
            s, d = os.path.join(cur_src, name), os.path.join(cur_dst, name)
            try:
                st = os.lstat(s)
            except OSError as exc:
                raise ToolError(f"path_changed during copy: {s}") from exc
            if stat.S_ISLNK(st.st_mode):
                link_text, errs = symlink_inside(s, norm(src))
                if errs:
                    raise ToolError(f"symlink blocked during copy: {','.join(errs)}")
                assert link_text is not None
                os.symlink(link_text, d)
                _meta_owner(d, st, follow=False)
                continue
            if stat.S_ISREG(st.st_mode):
                if st.st_nlink > 1:
                    raise ToolError("hard_link during copy")
                copy_file_nofollow(s, d, st, deadline)
                continue
            if stat.S_ISDIR(st.st_mode):
                os.mkdir(d, stat.S_IMODE(st.st_mode))
                _meta_owner(d, st, follow=True)
                walk(s, d)
                _meta_owner(d, st, follow=True)
                fsync_dir(d)
                continue
            raise ToolError(f"special_file during copy: {s}")
    walk(src, dst_payload)
    _meta_owner(dst_payload, root_st, follow=True)
    fsync_dir(dst_payload)
def verify_payload_against_manifest(payload: str, manifest: dict[str, Any], deadline: float) -> None:
    records, errors = candidate_inventory(payload, deadline)
    if errors:
        raise ToolError(f"destination inventory failed: {','.join(sorted(set(errors)))}")
    expected = manifest.get("inventory")
    if not isinstance(expected, list):
        raise ToolError("manifest inventory missing")
    fp = inventory_fingerprint(records)
    if fp != inventory_fingerprint(expected) or fp != manifest.get("inventory_fingerprint"):
        raise ToolError("destination_mismatch")
def write_manifest(path: str, payload: dict[str, Any]) -> None:
    ensure_no_symlink_components(path, "manifest")
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = ""
        fsync_dir(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp and os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
def remove_tree_nofollow(root: str, expected: os.stat_result) -> None:
    try:
        st0 = os.lstat(root)
    except OSError as exc:
        raise ToolError(f"tombstone missing before removal: {root}") from exc
    if st0.st_ino != expected.st_ino or st0.st_dev != expected.st_dev or stat.S_ISLNK(st0.st_mode):
        raise ToolError(f"tombstone identity mismatch: {root}")
    if stat.S_ISREG(st0.st_mode):
        os.unlink(root)
        return
    if not stat.S_ISDIR(st0.st_mode):
        raise ToolError(f"tombstone is special file: {root}")
    def walk_delete(path: str) -> None:
        try:
            st = os.lstat(path)
        except OSError as exc:
            raise ToolError(f"path changed during tombstone removal: {path}") from exc
        if stat.S_ISLNK(st.st_mode) or stat.S_ISREG(st.st_mode):
            os.unlink(path)
            return
        if not stat.S_ISDIR(st.st_mode):
            raise ToolError(f"special_file in tombstone: {path}")
        try:
            names = sorted(os.listdir(path))
        except OSError as exc:
            raise ToolError(f"cannot list tombstone dir: {path}") from exc
        for name in names:
            walk_delete(os.path.join(path, name))
        os.rmdir(path)
    walk_delete(root)
    if os.path.lexists(root):
        raise ToolError(f"tombstone still exists after removal: {root}")
def run_quarantine(
    *,
    source: str,
    candidate_token: str,
    run_id: str,
    tmp_root: str,
    quarantine_root: str,
    census_path: str,
    proc_root: str,
    runner: Callable[[list[str]], tuple[int, str, str]],
    now_fn: Callable[[], datetime],
    wait_s: float = SNAPSHOT_WAIT_S,
    poll_s: float = 0.25,
    data_root: str = DEFAULT_DATA_ROOT,
) -> dict[str, Any]:
    if not candidate_token:
        raise ToolError("candidate-token is required")
    if not is_uuid(run_id):
        raise ToolError("run-id must be a UUID")
    source = resolve_source(source, tmp_root)
    ensure_quarantine_root(quarantine_root)
    partial = os.path.join(quarantine_root, f".partial-{run_id}")
    final = os.path.join(quarantine_root, run_id)
    tomb = os.path.join(tmp_root, f".legacy-tmp-tombstone-{run_id}")
    for p, label in ((partial, "partial"), (final, "final"), (tomb, "tombstone")):
        if os.path.lexists(p):
            raise ToolError(f"refusing reused or existing {label} path: {p}")
    free_before = {"root": free_bytes("/"), "mnt_data": free_bytes(data_root)}
    classified = classify_source(
        source=source,
        tmp_root=tmp_root,
        census_path=census_path,
        proc_root=proc_root,
        runner=runner,
        now_fn=now_fn,
        wait_s=wait_s,
        poll_s=poll_s,
        started_at=now_fn(),
        data_root=data_root,
    )
    if classified.get("classification") != "eligible":
        raise ToolError(
            "quarantine revalidation failed: "
            f"{classified.get('classification')}: {','.join(classified.get('reasons') or [])}"
        )
    if classified.get("candidate_token") != candidate_token:
        raise ToolError("candidate token mismatch")
    if norm(classified["source"]["path"]) != norm(source):
        raise ToolError("source path mismatch")
    records, ident = classified.get("_inventory"), classified.get("_identity")
    if not isinstance(records, list) or not isinstance(ident, dict):
        raise ToolError("internal inventory missing after revalidation")
    producer = classified["source"]["recognized_producer"]
    if not isinstance(producer, str):
        raise ToolError("producer missing")
    try:
        if not identities_match(ident, identity_from_lstat(source)):
            raise ToolError("source changed before copy")
    except OSError as exc:
        raise ToolError(f"source unreadable before copy: {exc}") from exc
    mkdir_exact(partial, 0o700)
    payload = os.path.join(partial, "payload")
    deadline = time.monotonic() + INVENTORY_TIMEOUT_S
    copy_candidate_tree(source, payload, deadline)
    fsync_dir(partial)
    man = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "source_path": source,
        "source_basename": os.path.basename(source),
        "recognized_producer": producer,
        "candidate_token": candidate_token,
        "captured_at": utc_now_iso(),
        "source_identity": {
            k: ident[k]
            for k in ("type", "mode", "uid", "gid", "size", "mtime_ns", "nlink", "dev", "ino")
        },
        "inventory": [{k: v for k, v in r.items() if k not in ("dev", "ino")} for r in records],
        "inventory_fingerprint": inventory_fingerprint(records),
        "size_bytes": size_from_inventory(records),
    }
    write_manifest(os.path.join(partial, "manifest.json"), man)
    verify_payload_against_manifest(payload, man, time.monotonic() + INVENTORY_TIMEOUT_S)
    try:
        if not identities_match(ident, identity_from_lstat(source)):
            raise ToolError("source changed before final rename")
    except OSError as exc:
        raise ToolError(f"source unreadable before final rename: {exc}") from exc
    ensure_no_symlink_components(final, "final quarantine")
    if os.path.lexists(final):
        raise ToolError(f"final path appeared: {final}")
    os.rename(partial, final)
    fsync_dir(quarantine_root)
    free_after = {"root": free_bytes("/"), "mnt_data": free_bytes(data_root)}
    try:
        if not identities_match(ident, identity_from_lstat(source)):
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "quarantined_source_changed",
                "run_id": run_id,
                "source_path": source,
                "quarantine_path": final,
                "tombstone_path": None,
                "error": "source changed before tombstone rename",
                "free_bytes_before": free_before,
                "free_bytes_after": free_after,
            }
    except OSError as exc:
        raise ToolError(f"source unreadable before tombstone: {exc}") from exc
    if os.path.lexists(tomb):
        raise ToolError(f"tombstone path already exists: {tomb}")
    ensure_no_symlink_components(tomb, "tombstone")
    try:
        os.rename(source, tomb)
    except OSError as exc:
        raise ToolError(f"atomic source tombstone rename failed: {exc}") from exc
    try:
        t_st = os.lstat(tomb)
    except OSError as exc:
        raise ToolError(f"tombstone missing after rename: {exc}") from exc
    if t_st.st_ino != ident["ino"] or t_st.st_dev != ident["dev"]:
        raise ToolError("tombstone identity mismatch after rename")
    try:
        remove_tree_nofollow(tomb, t_st)
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "quarantined_pending_removal",
            "run_id": run_id,
            "source_path": source,
            "quarantine_path": final,
            "tombstone_path": tomb,
            "error": str(exc),
            "size_bytes": size_from_inventory(records),
            "free_bytes_before": free_before,
            "free_bytes_after": free_after,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "quarantined",
        "run_id": run_id,
        "source_path": source,
        "quarantine_path": final,
        "tombstone_path": None,
        "size_bytes": size_from_inventory(records),
        "manifest_path": os.path.join(final, "manifest.json"),
        "free_bytes_before": free_before,
        "free_bytes_after": free_after,
    }
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Probe or quarantine exactly one legacy /tmp producer candidate."
    )
    p.add_argument("--tmp-root", default=DEFAULT_TMP_ROOT)
    p.add_argument("--quarantine-root", default=DEFAULT_QUARANTINE_ROOT)
    p.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help=argparse.SUPPRESS)
    p.add_argument("--paseo-bin", default=os.environ.get("PASEO_BIN", "paseo"))
    p.add_argument("--census-path", default=DEFAULT_CENSUS)
    p.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    p.add_argument("--wait-seconds", type=float, default=SNAPSHOT_WAIT_S, help=argparse.SUPPRESS)
    p.add_argument("--poll-seconds", type=float, default=0.25, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="Classify one candidate (no mutation).")
    probe.add_argument("--source", required=True)
    q = sub.add_parser("quarantine", help="Quarantine exactly one eligible candidate.")
    q.add_argument("--source", required=True)
    q.add_argument("--candidate-token", required=True)
    q.add_argument("--run-id", required=True)
    return p
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = default_paseo_runner(args.paseo_bin)
    now_fn = lambda: datetime.now(timezone.utc)
    try:
        if args.command == "probe":
            result = run_probe(
                source=args.source,
                tmp_root=args.tmp_root,
                census_path=args.census_path,
                proc_root=args.proc_root,
                runner=runner,
                now_fn=now_fn,
                wait_s=args.wait_seconds,
                poll_s=args.poll_seconds,
                data_root=args.data_root,
            )
        elif args.command == "quarantine":
            result = run_quarantine(
                source=args.source,
                candidate_token=args.candidate_token,
                run_id=args.run_id,
                tmp_root=args.tmp_root,
                quarantine_root=args.quarantine_root,
                census_path=args.census_path,
                proc_root=args.proc_root,
                runner=runner,
                now_fn=now_fn,
                wait_s=args.wait_seconds,
                poll_s=args.poll_seconds,
                data_root=args.data_root,
            )
        else:
            raise ToolError(f"unknown command {args.command}")
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except ToolError as exc:
        print(f"legacy-tmp-quarantine: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"legacy-tmp-quarantine: {exc}", file=sys.stderr)
        return 1
if __name__ == "__main__":
    sys.exit(main())
