#!/usr/bin/env python3
"""Deterministic tests for legacy-tmp-quarantine.py. Temporary fixtures only."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "legacy-tmp-quarantine.py"
spec = importlib.util.spec_from_file_location("legacy_tmp_quarantine", SCRIPT)
assert spec and spec.loader
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
RUN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BOOT = "boot-test-legacy-tmp-001"


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def wjson(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def wfile(path: Path, content: str | bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


def make_stat(pid: int, comm: str, ticks: int) -> str:
    rest = ["0"] * 20
    rest[0], rest[1], rest[19] = "S", "1", str(ticks)
    return f"{pid} ({comm}) " + " ".join(rest) + "\n"


class FakePaseo:
    def __init__(self) -> None:
        self.inspect: dict[str, dict[str, Any]] = {}
        self.unarchived: list[dict[str, Any]] = []
        self.schedules: list[dict[str, Any]] = []
        self.schedule_identity: dict[str, dict[str, Any]] = {}
        self.permits: list[dict[str, Any]] = []
        self.terminals: list[dict[str, Any]] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        a = [x for x in args if x != "--json"]

        def ok(data: Any) -> tuple[int, str, str]:
            return 0, json.dumps(data), ""

        if a and a[0] == "inspect":
            aid = a[1]
            if aid not in self.inspect:
                return 1, "", f"agent not found: {aid}"
            return ok(self.inspect[aid])
        if a and a[0] == "ls" and "-g" in a:
            return ok(self.unarchived)
        if a[:2] == ["schedule", "ls"]:
            return ok(self.schedules)
        if a[:2] == ["schedule", "inspect"]:
            sid = a[2]
            if "--identity-only" not in a:
                return 1, "", "identity-only required"
            if sid not in self.schedule_identity:
                return 1, "", f"schedule not found: {sid}"
            return ok(self.schedule_identity[sid])
        if a[:2] == ["permit", "ls"]:
            return ok(self.permits)
        if a[:2] == ["terminal", "ls"]:
            return ok(self.terminals)
        return 1, "", f"unknown: {a}"


class H:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="legacy-tmp-q-")
        self.root = Path(self.tmp.name)
        self.tmp_root = self.root / "tmp"
        self.qroot = self.root / "quarantine" / "legacy-tmp"
        self.proc = self.root / "proc"
        self.census = self.root / "run" / "paseo" / "process-census.json"
        self.data_root = self.root / "mnt" / "data"
        for p in (self.tmp_root, self.qroot, self.proc, self.census.parent, self.data_root):
            p.mkdir(parents=True, exist_ok=True)
        self.paseo = FakePaseo()
        self.now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        self.processes: list[dict[str, Any]] = [
            {
                "pid": 1,
                "start_time_ticks": 10,
                "name": "init",
                "scope_complete": True,
                "references": [],
            }
        ]

    def close(self) -> None:
        for dp, _dns, fns in os.walk(self.root):
            try:
                os.chmod(dp, 0o755)
            except OSError:
                pass
            for n in fns:
                try:
                    os.chmod(os.path.join(dp, n), 0o644)
                except OSError:
                    pass
        self.tmp.cleanup()

    def seed_proc(self) -> None:
        if self.proc.exists():
            for dp, dns, fns in os.walk(self.proc, topdown=False):
                for n in fns:
                    Path(dp, n).unlink(missing_ok=True)
                for n in dns:
                    try:
                        Path(dp, n).rmdir()
                    except OSError:
                        pass
        self.proc.mkdir(exist_ok=True)
        wfile(self.proc / "sys/kernel/random/boot_id", BOOT + "\n")
        for p in self.processes:
            pdir = self.proc / str(p["pid"])
            pdir.mkdir(parents=True, exist_ok=True)
            wfile(pdir / "stat", make_stat(p["pid"], p.get("name", "app"), p["start_time_ticks"]))
            if p.get("kernel_thread"):
                wfile(pdir / "cmdline", b"")
            else:
                wfile(pdir / "cmdline", f"{p.get('name', 'app')}\0".encode())
                try:
                    os.symlink("/bin/true", pdir / "exe")
                except FileExistsError:
                    pass

    def write_census(
        self,
        captured_at: str | None = None,
        complete: bool = True,
        roots: list[str] | None = None,
        processes: list[dict[str, Any]] | None = None,
    ) -> None:
        src = processes if processes is not None else self.processes
        recs = []
        for p in src:
            if p.get("kernel_thread"):
                continue
            recs.append(
                {
                    "pid": p["pid"],
                    "start_time_ticks": p["start_time_ticks"],
                    "uid": 1000,
                    "name": p.get("name", "app"),
                    "scope_complete": p.get("scope_complete", True),
                    "references": p.get("references", []),
                }
            )
        wjson(
            self.census,
            {
                "schema_version": 1,
                "boot_id": BOOT,
                "captured_at": captured_at or iso(self.now),
                "roots": roots if roots is not None else [str(self.tmp_root)],
                "complete": complete,
                "errors": [],
                "processes": recs,
            },
        )
        self.seed_proc()

    def put_candidate(
        self,
        name: str = "paseo-legacy-a",
        *,
        body: bytes = b"legacy-payload",
        as_file: bool = False,
    ) -> Path:
        path = self.tmp_root / name
        if as_file:
            wfile(path, body)
        else:
            path.mkdir(parents=True, exist_ok=True)
            wfile(path / "data.bin", body)
            wfile(path / "nested" / "x.txt", b"nested")
        return path

    def mark_agent(self, agent_id: str = A, cwd: str | None = None) -> None:
        self.paseo.inspect[agent_id] = {
            "Id": agent_id,
            "Status": "running",
            "Cwd": cwd or str(self.root / "workspace"),
            "ParentAgentId": None,
        }
        if not any(x.get("id") == agent_id for x in self.paseo.unarchived):
            self.paseo.unarchived.append({"id": agent_id, "status": "running"})

    def probe(
        self,
        source: Path | str,
        started_offset: float = -1.0,
        *,
        profile: str = M.DEFAULT_PROFILE,
    ) -> dict[str, Any]:
        return M.run_probe(
            source=str(source),
            tmp_root=str(self.tmp_root),
            census_path=str(self.census),
            proc_root=str(self.proc),
            runner=self.paseo,
            now_fn=lambda: self.now,
            wait_s=0.0,
            poll_s=0.01,
            started_at=self.now + timedelta(seconds=started_offset),
            data_root=str(self.data_root),
            profile=profile,
        )

    def classify(
        self,
        source: Path | str,
        *,
        profile: str = M.DEFAULT_PROFILE,
    ) -> dict[str, Any]:
        return M.classify_source(
            source=str(source),
            tmp_root=str(self.tmp_root),
            census_path=str(self.census),
            proc_root=str(self.proc),
            runner=self.paseo,
            now_fn=lambda: self.now,
            wait_s=0.0,
            poll_s=0.01,
            started_at=self.now - timedelta(seconds=1),
            data_root=str(self.data_root),
            profile=profile,
        )

    def quarantine(
        self,
        source: Path | str,
        token: str,
        run_id: str = RUN,
        now_shift: float = 5.0,
        *,
        profile: str = M.DEFAULT_PROFILE,
    ) -> dict[str, Any]:
        # Fresh post-start census for second complete proof.
        self.write_census(captured_at=iso(self.now + timedelta(seconds=now_shift + 1)))
        return M.run_quarantine(
            source=str(source),
            candidate_token=token,
            run_id=run_id,
            tmp_root=str(self.tmp_root),
            quarantine_root=str(self.qroot),
            census_path=str(self.census),
            proc_root=str(self.proc),
            runner=self.paseo,
            now_fn=lambda: self.now + timedelta(seconds=now_shift),
            wait_s=0.0,
            poll_s=0.01,
            data_root=str(self.data_root),
            profile=profile,
        )

    def eligible(self, name: str = "paseo-legacy-a", **kwargs: Any) -> tuple[Path, dict[str, Any]]:
        src = self.put_candidate(name, **kwargs)
        self.write_census()
        result = self.probe(src)
        return src, result


class PreRuntimeH(H):
    """Fixture for closed pre-runtime-scratch-layout profile (patched exact roots)."""

    def __init__(self) -> None:
        super().__init__()
        self.scratch = self.root / "mnt" / "data" / "paseo-runtime" / "scratch"
        self.locks = self.root / "mnt" / "data" / "paseo-runtime" / "locks"
        self.qroot = (
            self.root
            / "mnt"
            / "data"
            / "paseo-runtime"
            / "quarantine"
            / "pre-runtime-scratch-layout"
        )
        self.tmp_root = self.scratch
        for p in (self.scratch, self.locks, self.qroot):
            p.mkdir(parents=True, exist_ok=True)
        self._patches = [
            mock.patch.object(M, "PRE_RUNTIME_SOURCE_ROOT", str(self.scratch)),
            mock.patch.object(M, "PRE_RUNTIME_QUARANTINE_ROOT", str(self.qroot)),
            mock.patch.object(M, "PRE_RUNTIME_LOCK_ROOT", str(self.locks)),
        ]
        for p in self._patches:
            p.start()

    def close(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        super().close()

    def put_candidate(
        self,
        name: str = "test-runner",
        *,
        body: bytes = b"legacy-payload",
        as_file: bool = False,
    ) -> Path:
        return super().put_candidate(name, body=body, as_file=as_file)

    def probe(self, source: Path | str, started_offset: float = -1.0, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("profile", M.PROFILE_PRE_RUNTIME_SCRATCH)
        return super().probe(source, started_offset, **kwargs)

    def classify(self, source: Path | str, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("profile", M.PROFILE_PRE_RUNTIME_SCRATCH)
        return super().classify(source, **kwargs)

    def quarantine(
        self,
        source: Path | str,
        token: str,
        run_id: str = RUN,
        now_shift: float = 5.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        kwargs.setdefault("profile", M.PROFILE_PRE_RUNTIME_SCRATCH)
        return super().quarantine(source, token, run_id=run_id, now_shift=now_shift, **kwargs)

    def eligible(self, name: str = "test-runner", **kwargs: Any) -> tuple[Path, dict[str, Any]]:
        src = self.put_candidate(name, **kwargs)
        self.write_census(roots=[str(self.scratch)])
        result = self.probe(src)
        return src, result

    def write_census(
        self,
        captured_at: str | None = None,
        complete: bool = True,
        roots: list[str] | None = None,
        processes: list[dict[str, Any]] | None = None,
    ) -> None:
        super().write_census(
            captured_at=captured_at,
            complete=complete,
            roots=roots if roots is not None else [str(self.scratch)],
            processes=processes,
        )


class Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = H()

    def tearDown(self) -> None:
        self.h.close()

    def test_allowed_producer_eligible(self) -> None:
        for name in (
            "paseo-x",
            "shab-stage-runner-1",
            "warm-live-api-z",
            "grok-run-a",
            "ask-expert-b",
            "expert-mcp-c",
            "cxc-d",
        ):
            with self.subTest(name=name):
                h = H()
                try:
                    src, result = h.eligible(name)
                    self.assertEqual(result["classification"], "eligible", result)
                    self.assertTrue(result["candidate_token"])
                    self.assertEqual(result["source"]["path"], str(src))
                    self.assertIsNotNone(result["source"]["recognized_producer"])
                    self.assertIn("root", result["free_bytes"])
                    self.assertIn("mnt_data", result["free_bytes"])
                finally:
                    h.close()

    def test_unknown_root_never_eligible(self) -> None:
        src = self.h.put_candidate("random-scratch-dir")
        self.h.write_census()
        result = self.h.probe(src)
        self.assertEqual(result["classification"], "unknown")
        self.assertIn("unknown_producer", result["reasons"])
        self.assertIsNone(result["candidate_token"])
        self.assertTrue(src.exists())

    def test_probe_never_mutates(self) -> None:
        src, result = self.h.eligible()
        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(src.exists())
        self.assertEqual(list(self.h.qroot.iterdir()), [])
        leftovers = [p for p in self.h.tmp_root.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_protected_process_reference(self) -> None:
        src = self.h.put_candidate()
        self.h.processes.append(
            {
                "pid": 42,
                "start_time_ticks": 99,
                "name": "worker",
                "scope_complete": True,
                "references": [{"kind": "cwd", "path": str(src)}],
            }
        )
        self.h.write_census()
        result = self.h.probe(src)
        self.assertEqual(result["classification"], "protected")
        self.assertIn("process_reference_under_candidate", result["reasons"])

    def test_protected_agent_cwd(self) -> None:
        src = self.h.put_candidate()
        self.h.mark_agent(A, cwd=str(src / "nested"))
        self.h.write_census()
        result = self.h.probe(src)
        self.assertEqual(result["classification"], "protected")
        self.assertIn("agent_cwd_under_candidate", result["reasons"])

    def test_protected_schedule_target_cwd(self) -> None:
        src = self.h.put_candidate()
        # Unarchived agent elsewhere; schedule targets agent B whose cwd is under candidate.
        self.h.mark_agent(A, cwd=str(self.h.root / "other"))
        self.h.paseo.inspect[B] = {
            "Id": B,
            "Status": "running",
            "Cwd": str(src),
            "ParentAgentId": None,
        }
        self.h.paseo.schedules.append(
            {"id": "s1", "status": "active", "target": f"agent:{B[:7]}"}
        )
        self.h.paseo.schedule_identity["s1"] = {
            "id": "s1",
            "target": {"type": "agent", "agentId": B},
            "status": "active",
        }
        self.h.write_census()
        result = self.h.probe(src)
        self.assertEqual(result["classification"], "protected")
        self.assertIn("active_schedule_target_cwd_under_candidate", result["reasons"])

    def test_active_new_agent_schedule_blocks_candidate(self) -> None:
        src = self.h.put_candidate()
        self.h.mark_agent(A, cwd=str(self.h.root / "other"))
        self.h.paseo.schedules.append(
            {"id": "s-new", "status": "active", "target": "new-agent"}
        )
        self.h.paseo.schedule_identity["s-new"] = {
            "id": "s-new",
            "target": {"type": "new-agent", "config": {"provider": "claude", "cwd": "/ws"}},
            "status": "active",
        }
        self.h.write_census()
        result = self.h.probe(src)
        self.assertEqual(result["classification"], "blocked")
        self.assertIn("active_new_agent_schedule", result["reasons"])

    def test_protected_terminal_and_permit(self) -> None:
        src = self.h.put_candidate()
        self.h.mark_agent(A, cwd=str(self.h.root / "ws"))
        self.h.paseo.terminals.append({"id": "t1", "cwd": str(src / "data.bin")})
        self.h.write_census()
        r1 = self.h.probe(src)
        self.assertEqual(r1["classification"], "protected")
        self.assertIn("terminal_cwd_under_candidate", r1["reasons"])

        self.h.paseo.terminals.clear()
        self.h.paseo.permits.append({"id": "p1", "agentId": A})
        # Move agent cwd under candidate for permit path.
        self.h.paseo.inspect[A]["Cwd"] = str(src)
        self.h.write_census()
        r2 = self.h.probe(src)
        self.assertEqual(r2["classification"], "protected")
        self.assertIn("permitted_agent_cwd_under_candidate", r2["reasons"])

    def test_snapshot_stale_incomplete_unrelated(self) -> None:
        src = self.h.put_candidate()
        self.h.write_census(complete=False)
        r = self.h.probe(src)
        self.assertEqual(r["classification"], "blocked")
        self.assertTrue(any("incomplete" in x or "snapshot" in x for x in r["reasons"]))

        self.h.write_census(captured_at=iso(self.h.now - timedelta(seconds=120)))
        r2 = self.h.probe(src)
        self.assertEqual(r2["classification"], "blocked")
        self.assertIn("snapshot_stale", r2["reasons"])

        self.h.write_census(roots=["/var/unrelated"])
        r3 = self.h.probe(src)
        self.assertEqual(r3["classification"], "blocked")
        self.assertIn("snapshot_roots_unrelated", r3["reasons"])

    def test_special_escaping_symlink_hardlink(self) -> None:
        # Top-level symlink
        src = self.h.tmp_root / "paseo-link"
        os.symlink(str(self.h.root / "outside"), src)
        self.h.write_census()
        r = self.h.probe(src)
        self.assertIn("root_is_symlink", r["reasons"])

        # Escaping nested relative symlink
        src2 = self.h.put_candidate("paseo-escape")
        os.symlink("../../outside", src2 / "evil")
        self.h.write_census()
        r2 = self.h.probe(src2)
        self.assertEqual(r2["classification"], "blocked")
        self.assertIn("symlink_escape", r2["reasons"])

        # Hard link
        src3 = self.h.put_candidate("paseo-hard", as_file=False)
        target = src3 / "data.bin"
        link = src3 / "hard.bin"
        try:
            os.link(target, link)
        except OSError:
            self.skipTest("hard links not supported on this filesystem")
        self.h.write_census()
        r3 = self.h.probe(src3)
        self.assertIn("hard_link", r3["reasons"])

        # Special FIFO
        src4 = self.h.tmp_root / "paseo-fifo"
        src4.mkdir()
        fifo = src4 / "f"
        os.mkfifo(fifo)
        self.h.write_census()
        r4 = self.h.probe(src4)
        self.assertIn("special_file", r4["reasons"])

    def test_absolute_symlink_blocked(self) -> None:
        src = self.h.put_candidate("paseo-abs-link")
        # Absolute link text even when real target is inside the candidate.
        os.symlink(str(src / "data.bin"), src / "abs-link")
        self.h.write_census()
        r = self.h.probe(src)
        self.assertEqual(r["classification"], "blocked")
        self.assertIn("absolute_symlink", r["reasons"])
        self.assertIsNone(r["candidate_token"])

    def test_inventory_change_and_timeout(self) -> None:
        src = self.h.put_candidate("paseo-change")
        self.h.write_census()
        real_inv = M.candidate_inventory

        def flip_then_inv(root: str, deadline: float):
            # Mutate between the two dual-inventory passes via counter.
            if not hasattr(flip_then_inv, "n"):
                flip_then_inv.n = 0  # type: ignore[attr-defined]
            flip_then_inv.n += 1  # type: ignore[attr-defined]
            if flip_then_inv.n == 2:  # type: ignore[attr-defined]
                wfile(Path(root) / "mutated", b"changed")
            return real_inv(root, deadline)

        with mock.patch.object(M, "candidate_inventory", side_effect=flip_then_inv):
            r = self.h.probe(src)
        self.assertEqual(r["classification"], "blocked")
        self.assertIn("tree_changed", r["reasons"])

        def timeout_inv(root: str, deadline: float):
            return [], ["inventory_timeout"]

        with mock.patch.object(M, "candidate_inventory", side_effect=timeout_inv):
            r2 = self.h.probe(src)
        self.assertIn("inventory_timeout", r2["reasons"])

    def test_verified_copy_and_manifest(self) -> None:
        src, probe = self.h.eligible("paseo-copy-me", body=b"hello-legacy")
        self.assertEqual(probe["classification"], "eligible")
        token = probe["candidate_token"]
        result = self.h.quarantine(src, token)
        self.assertEqual(result["status"], "quarantined", result)
        self.assertFalse(src.exists())
        final = Path(result["quarantine_path"])
        self.assertTrue(final.is_dir())
        self.assertEqual(result["manifest_path"], str(final / "manifest.json"))
        self.assertEqual(result["recovery_authority"], str(final))
        self.assertIn("free_bytes_before", result)
        self.assertIn("free_bytes_after", result)
        self.assertIsNone(result["tombstone_path"])
        man = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(man["run_id"], RUN)
        self.assertEqual(man["source_path"], str(src))
        self.assertEqual(man["candidate_token"], token)
        self.assertTrue(man["inventory_fingerprint"])
        self.assertTrue((final / "payload" / "data.bin").is_file())
        self.assertEqual((final / "payload" / "data.bin").read_bytes(), b"hello-legacy")
        # Internal safe relative symlink preserved
        src2 = self.h.put_candidate("paseo-with-link")
        os.symlink("data.bin", src2 / "rel-link")
        self.h.write_census()
        p2 = self.h.probe(src2)
        self.assertEqual(p2["classification"], "eligible", p2)
        run2 = str(uuid.uuid4())
        r2 = self.h.quarantine(src2, p2["candidate_token"], run_id=run2)
        self.assertEqual(r2["status"], "quarantined")
        link = Path(r2["quarantine_path"]) / "payload" / "rel-link"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "data.bin")

    def test_reused_run_id_refused(self) -> None:
        src, probe = self.h.eligible("paseo-reuse")
        token = probe["candidate_token"]
        r1 = self.h.quarantine(src, token)
        self.assertEqual(r1["status"], "quarantined")
        src2 = self.h.put_candidate("paseo-reuse-2")
        self.h.write_census()
        p2 = self.h.probe(src2)
        with self.assertRaises(M.ToolError) as ctx:
            self.h.quarantine(src2, p2["candidate_token"], run_id=RUN)
        self.assertIn("reused or existing", str(ctx.exception))

    def test_exact_path_and_token_enforcement(self) -> None:
        src, probe = self.h.eligible("paseo-token")
        token = probe["candidate_token"]
        with self.assertRaises(M.ToolError):
            self.h.quarantine(src, "deadbeef" * 8)
        other = self.h.put_candidate("paseo-other")
        self.h.write_census()
        with self.assertRaises(M.ToolError):
            # Token for src must not work for other path even if we force token.
            self.h.quarantine(other, token)
        # Non-direct child
        nested = src / "nested"
        with self.assertRaises(M.ToolError):
            self.h.probe(nested)

    def test_destination_mismatch(self) -> None:
        src, probe = self.h.eligible("paseo-dest-mismatch")
        token = probe["candidate_token"]
        real_verify = M.verify_payload_against_manifest

        def bad_verify(payload: str, manifest: dict[str, Any], deadline: float) -> None:
            raise M.ToolError("destination_mismatch")

        with mock.patch.object(M, "verify_payload_against_manifest", side_effect=bad_verify):
            with self.assertRaises(M.ToolError) as ctx:
                self.h.quarantine(src, token)
            self.assertIn("destination_mismatch", str(ctx.exception))
        # Source untouched; partial may remain (never auto-deleted).
        self.assertTrue(src.exists())
        partials = list(self.h.qroot.glob(".partial-*"))
        self.assertTrue(partials)
        # Final must not exist.
        self.assertFalse((self.h.qroot / RUN).exists())
        del real_verify

    def test_source_change_before_rename(self) -> None:
        src, probe = self.h.eligible("paseo-src-change", body=b"stable")
        token = probe["candidate_token"]
        real_copy = M.copy_candidate_tree

        def copy_and_mutate(source: str, dst: str, deadline: float) -> None:
            real_copy(source, dst, deadline)
            wfile(Path(source) / "extra-after-copy", b"race")

        with mock.patch.object(M, "copy_candidate_tree", side_effect=copy_and_mutate):
            with self.assertRaises(M.ToolError) as ctx:
                self.h.quarantine(src, token)
            self.assertIn("source changed", str(ctx.exception).lower())
        self.assertTrue(src.exists())

    def test_partial_removal_with_recoverable_durable_copy(self) -> None:
        src, probe = self.h.eligible("paseo-pending-rm")
        token = probe["candidate_token"]

        def fail_remove(root: str, expected: os.stat_result) -> None:
            raise M.ToolError("simulated removal failure")

        with mock.patch.object(M, "remove_tree_nofollow", side_effect=fail_remove):
            result = self.h.quarantine(src, token)
        self.assertEqual(result["status"], "quarantined_pending_removal")
        self.assertTrue(Path(result["quarantine_path"]).is_dir())
        self.assertTrue(Path(result["tombstone_path"]).exists())
        self.assertEqual(result["recovery_authority"], result["quarantine_path"])
        self.assertEqual(
            result["manifest_path"],
            str(Path(result["quarantine_path"]) / "manifest.json"),
        )
        self.assertIn("free_bytes_before", result)
        self.assertIn("free_bytes_after", result)
        self.assertIsNotNone(result["size_bytes"])
        # Durable copy is recovery authority.
        man = Path(result["quarantine_path"]) / "manifest.json"
        self.assertTrue(man.is_file())
        self.assertFalse(src.exists())

    def test_content_change_after_durable_copy_preserves_both(self) -> None:
        """Nested content change after durable publish must not delete source.

        Root directory identity can stay unchanged when only file bytes change;
        post-publish census + dual inventory + exact token must refuse tombstone.
        """
        src, probe = self.h.eligible("paseo-post-content", body=b"original-bytes")
        token = probe["candidate_token"]
        root_before = os.lstat(src)
        real_rename = os.rename

        def rename_then_mutate(src_path: str, dst_path: str) -> None:
            real_rename(src_path, dst_path)
            if os.path.basename(src_path).startswith(".partial-"):
                # Change nested file content without replacing the root inode.
                wfile(src / "data.bin", b"mutated-after-durable-publish")
                root_after = os.lstat(src)
                self.assertEqual(root_after.st_ino, root_before.st_ino)
                self.assertEqual(root_after.st_dev, root_before.st_dev)

        with mock.patch.object(os, "rename", side_effect=rename_then_mutate):
            result = self.h.quarantine(src, token)
        self.assertEqual(result["status"], "quarantined_source_preserved", result)
        self.assertTrue(src.exists(), "original source must be preserved")
        self.assertTrue(Path(result["quarantine_path"]).is_dir())
        self.assertIsNone(result["tombstone_path"])
        self.assertEqual(result["recovery_authority"], result["quarantine_path"])
        self.assertTrue(Path(result["manifest_path"]).is_file())
        self.assertIn("free_bytes_after", result)
        # Durable copy still holds pre-mutation bytes.
        payload = Path(result["quarantine_path"]) / "payload" / "data.bin"
        self.assertEqual(payload.read_bytes(), b"original-bytes")
        self.assertEqual((src / "data.bin").read_bytes(), b"mutated-after-durable-publish")

    def test_replacement_inode_token_mismatch(self) -> None:
        src, probe = self.h.eligible("paseo-inode", body=b"same-bytes")
        token = probe["candidate_token"]
        self.assertTrue(token)
        # Replace path with a new inode; content and metadata-ish shape match.
        if src.is_dir():
            for child in src.rglob("*"):
                if child.is_file():
                    child.unlink()
            for child in sorted(src.rglob("*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
            src.rmdir()
        else:
            src.unlink()
        src2 = self.h.put_candidate("paseo-inode", body=b"same-bytes")
        self.assertEqual(src2, src)
        self.h.write_census()
        p2 = self.h.probe(src2)
        self.assertEqual(p2["classification"], "eligible", p2)
        self.assertNotEqual(p2["candidate_token"], token)
        with self.assertRaises(M.ToolError) as ctx:
            self.h.quarantine(src2, token)
        self.assertIn("token", str(ctx.exception).lower())

    def test_quarantine_root_symlink_parent_fail_closed(self) -> None:
        real_parent = self.h.root / "real-q-parent"
        real_parent.mkdir(parents=True, exist_ok=True)
        link_parent = self.h.root / "q-link-parent"
        os.symlink(str(real_parent), link_parent)
        bad_qroot = link_parent / "legacy-tmp"
        with self.assertRaises(M.ToolError) as ctx:
            M.ensure_quarantine_root(str(bad_qroot))
        self.assertTrue(
            "symlink" in str(ctx.exception).lower() or "real directory" in str(ctx.exception).lower(),
            ctx.exception,
        )
        # Source fixtures untouched.
        src = self.h.put_candidate("paseo-symlink-parent")
        self.assertTrue(src.exists())

    def test_file_candidate_and_cli_main(self) -> None:
        src, probe = self.h.eligible("paseo-file-only", as_file=True, body=b"flat")
        self.assertEqual(probe["classification"], "eligible")
        token = probe["candidate_token"]
        result = self.h.quarantine(src, token, run_id=str(uuid.uuid4()))
        self.assertEqual(result["status"], "quarantined")
        payload = Path(result["quarantine_path"]) / "payload"
        self.assertTrue(payload.is_file())
        self.assertEqual(payload.read_bytes(), b"flat")

        # CLI smoke: probe via main (temp fixtures only).
        src2 = self.h.put_candidate("paseo-cli")
        self.h.write_census(captured_at=iso(self.h.now))
        rc = M.main(
            [
                "--tmp-root",
                str(self.h.tmp_root),
                "--quarantine-root",
                str(self.h.qroot),
                "--data-root",
                str(self.h.data_root),
                "--census-path",
                str(self.h.census),
                "--proc-root",
                str(self.h.proc),
                "--wait-seconds",
                "0",
                "--poll-seconds",
                "0.01",
                "probe",
                "--source",
                str(src2),
            ]
        )
        # main uses wall clock for started_at; snapshot may be pre_start/stale vs wall clock.
        # Exercise argv path only — non-zero is acceptable when clock differs.
        self.assertIn(rc, (0, 2))

    def test_snapshot_symlink_blocks(self) -> None:
        src = self.h.put_candidate()
        self.h.write_census()
        # Replace census with symlink
        real = self.h.census
        bak = self.h.root / "census-real.json"
        real.replace(bak)
        os.symlink(bak, real)
        r = self.h.probe(src)
        self.assertEqual(r["classification"], "blocked")
        self.assertTrue(
            any("symlink" in x for x in r["reasons"]) or any("snapshot" in x for x in r["reasons"])
        )

    def test_no_glob_and_one_candidate(self) -> None:
        # Tool requires exact path; wildcards are literal basenames.
        weird = self.h.tmp_root / "paseo-*"
        weird.mkdir()
        wfile(weird / "a", b"1")
        self.h.write_census()
        r = self.h.probe(weird)
        self.assertEqual(r["classification"], "eligible")
        self.assertEqual(r["source"]["basename"], "paseo-*")

    def test_default_proof_window_is_75s(self) -> None:
        self.assertEqual(M.SNAPSHOT_WAIT_S, 75)

    def test_transient_process_new_retries_with_newer_snapshot(self) -> None:
        src = self.h.put_candidate()
        self.h.write_census(captured_at=iso(self.h.now))
        pdir = self.h.proc / "77"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(77, "new", 1))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")

        sleep_n = {"n": 0}

        def on_sleep(_s: float) -> None:
            sleep_n["n"] += 1
            if sleep_n["n"] == 1:
                self.h.processes = [
                    {
                        "pid": 1,
                        "start_time_ticks": 10,
                        "name": "init",
                        "scope_complete": True,
                        "references": [],
                    },
                    {
                        "pid": 77,
                        "start_time_ticks": 1,
                        "name": "new",
                        "scope_complete": True,
                        "references": [],
                    },
                ]
                self.h.write_census(
                    captured_at=iso(self.h.now + timedelta(seconds=1)),
                    roots=[str(src)],
                )

        with mock.patch.object(M._asc.time, "sleep", side_effect=on_sleep):
            result = M.run_probe(
                source=str(src),
                tmp_root=str(self.h.tmp_root),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=2.0,
                poll_s=0.05,
                started_at=self.h.now - timedelta(seconds=1),
                data_root=str(self.h.data_root),
            )
        self.assertGreaterEqual(sleep_n["n"], 1)
        self.assertEqual(result["classification"], "eligible", result)
        self.assertEqual(result["process_census"]["status"], "ok")

    def test_persistent_transient_blocks_with_exact_final_reasons(self) -> None:
        src = self.h.put_candidate()
        self.h.write_census()
        pdir = self.h.proc / "77"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(77, "new", 1))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")
        with mock.patch.object(M._asc.time, "sleep", return_value=None):
            result = M.run_probe(
                source=str(src),
                tmp_root=str(self.h.tmp_root),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=0.15,
                poll_s=0.01,
                started_at=self.h.now - timedelta(seconds=1),
                data_root=str(self.h.data_root),
            )
        self.assertEqual(result["classification"], "blocked")
        self.assertIn("process_new", result["reasons"])
        self.assertIn("process_new", result["process_census"]["reasons"])

    def test_mixed_transient_nontransient_blocks_immediately(self) -> None:
        src = self.h.put_candidate()
        self.h.write_census(roots=["/var/unrelated"])
        pdir = self.h.proc / "77"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(77, "new", 1))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")
        sleep_calls: list[float] = []
        with mock.patch.object(M._asc.time, "sleep", side_effect=lambda s: sleep_calls.append(s)):
            result = M.run_probe(
                source=str(src),
                tmp_root=str(self.h.tmp_root),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=2.0,
                poll_s=0.05,
                started_at=self.h.now - timedelta(seconds=1),
                data_root=str(self.h.data_root),
            )
        self.assertEqual(result["classification"], "blocked")
        self.assertIn("snapshot_roots_unrelated", result["reasons"])
        self.assertIn("process_new", result["reasons"])
        self.assertEqual(sleep_calls, [])

    def test_process_new_still_blocks_when_wait_zero(self) -> None:
        src = self.h.put_candidate()
        self.h.write_census()
        pdir = self.h.proc / "77"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(77, "new", 1))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")
        result = self.h.probe(src)
        self.assertEqual(result["classification"], "blocked")
        self.assertIn("process_new", result["reasons"])

    def test_legacy_profile_lock_not_applicable_and_protection_on_eligible(self) -> None:
        src, result = self.h.eligible("paseo-prot")
        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(result["profile"], M.PROFILE_LEGACY_TMP)
        self.assertEqual(result["lock_evidence"]["status"], "not_applicable")
        self.assertIsNotNone(result["protection_fingerprint"])
        self.assertIsNotNone(result["protection_evidence"])
        self.assertEqual(result["protection_evidence"]["lock"]["status"], "not_applicable")
        self.assertEqual(result["protection_evidence"]["process_references"], [])
        self.assertEqual(result["protection_evidence"]["paseo_protection_reasons"], [])
        self.assertEqual(result["protection_evidence"]["boot_id"], BOOT)
        # Volatile captured_at must not be part of the fingerprint payload.
        self.assertNotIn("captured_at", result["protection_evidence"])
        self.assertNotIn("captured_at", result["protection_evidence"]["process_proof"])
        del src

    def test_dual_inventory_fresh_deadline_per_walk(self) -> None:
        """Each of the two inventory walks receives a fresh absolute 60s bound."""
        src = self.h.put_candidate("paseo-deadline")
        self.h.write_census()
        deadlines: list[float] = []
        mono = {"t": 1000.0}
        real_inv = M.candidate_inventory

        def capture_inv(root: str, deadline: float):
            deadlines.append(deadline)
            # Simulate first walk consuming most of a shared budget.
            if len(deadlines) == 1:
                mono["t"] = 1050.0
            return real_inv(root, deadline)

        with mock.patch.object(M.time, "monotonic", side_effect=lambda: mono["t"]):
            with mock.patch.object(M, "candidate_inventory", side_effect=capture_inv):
                r1, e1 = M.dual_inventory(str(src))
        self.assertEqual(e1, [])
        self.assertTrue(r1)
        self.assertEqual(len(deadlines), 2)
        self.assertEqual(deadlines[0], 1000.0 + M.INVENTORY_TIMEOUT_S)
        # Second walk must not inherit remaining budget from the first.
        self.assertEqual(deadlines[1], 1050.0 + M.INVENTORY_TIMEOUT_S)
        self.assertEqual(deadlines[1] - deadlines[0], 50.0)

    def test_protection_fingerprint_stable_across_captured_at(self) -> None:
        src = self.h.put_candidate("paseo-fp-stable")
        self.h.write_census(captured_at=iso(self.h.now - timedelta(seconds=2)))
        r1 = self.h.probe(src, started_offset=-30.0)
        self.assertEqual(r1["classification"], "eligible", r1)
        fp1 = r1["protection_fingerprint"]
        token1 = r1["candidate_token"]
        # Fresh captured_at, same boot and protections → same protection fingerprint.
        self.h.write_census(captured_at=iso(self.h.now - timedelta(seconds=10)))
        r2 = self.h.probe(src, started_offset=-30.0)
        self.assertEqual(r2["classification"], "eligible", r2)
        self.assertEqual(r2["protection_fingerprint"], fp1)
        self.assertEqual(r2["candidate_token"], token1)
        self.assertNotEqual(r1["process_census"]["captured_at"], r2["process_census"]["captured_at"])

    def test_protection_fingerprint_changes_with_owner(self) -> None:
        src, r1 = self.h.eligible("paseo-fp-owner")
        fp1 = r1["protection_fingerprint"]
        token1 = r1["candidate_token"]
        # Mutate root owner evidence (mtime) without changing tree inventory content.
        os.utime(src, ns=(1_000_000_000, 2_000_000_000))
        self.h.write_census()
        r2 = self.h.probe(src)
        self.assertEqual(r2["classification"], "eligible", r2)
        self.assertNotEqual(r2["owner_evidence"]["mtime_ns"], r1["owner_evidence"]["mtime_ns"])
        self.assertNotEqual(r2["protection_fingerprint"], fp1)
        self.assertNotEqual(r2["candidate_token"], token1)


class PreRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = PreRuntimeH()

    def tearDown(self) -> None:
        self.h.close()

    def test_accepts_only_exact_basenames(self) -> None:
        for name in ("test-runner", "verify"):
            with self.subTest(name=name):
                h = PreRuntimeH()
                try:
                    src, result = h.eligible(name)
                    self.assertEqual(result["classification"], "eligible", result)
                    self.assertEqual(result["profile"], M.PROFILE_PRE_RUNTIME_SCRATCH)
                    self.assertEqual(
                        result["source"]["recognized_producer"],
                        M.PRE_RUNTIME_PRODUCER,
                    )
                    self.assertEqual(result["lock_evidence"]["status"], "absent")
                    self.assertTrue(result["protection_fingerprint"])
                    self.assertEqual(
                        result["protection_evidence"]["lock"]["status"], "absent"
                    )
                    self.assertTrue(src.exists())
                finally:
                    h.close()

    def test_rejects_other_basenames_and_generic_tmp(self) -> None:
        for name in (
            "build",
            "package",
            "review-fix",
            str(uuid.uuid4()),
            "paseo-legacy-a",
            "scratch-other",
        ):
            with self.subTest(name=name):
                src = self.h.put_candidate(name)
                self.h.write_census()
                result = self.h.probe(src)
                self.assertEqual(result["classification"], "unknown", result)
                self.assertIn("unknown_producer", result["reasons"])
                self.assertIsNone(result["candidate_token"])
                self.assertEqual(result["profile"], M.PROFILE_PRE_RUNTIME_SCRATCH)
                self.assertTrue(src.exists())

        # Generic /tmp path is not a direct child of the closed scratch root.
        other = self.h.root / "tmp-generic" / "paseo-x"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.mkdir()
        with self.assertRaises(M.ToolError):
            self.h.probe(other)

    def test_root_override_fail_closed(self) -> None:
        src = self.h.put_candidate("test-runner")
        self.h.write_census()
        with self.assertRaises(M.ToolError) as ctx:
            M.run_probe(
                source=str(src),
                tmp_root="/tmp",
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=0.0,
                poll_s=0.01,
                started_at=self.h.now - timedelta(seconds=1),
                data_root=str(self.h.data_root),
                profile=M.PROFILE_PRE_RUNTIME_SCRATCH,
            )
        self.assertIn("requires source root", str(ctx.exception))
        with self.assertRaises(M.ToolError) as ctx2:
            M.run_quarantine(
                source=str(src),
                candidate_token="x" * 64,
                run_id=RUN,
                tmp_root=str(self.h.scratch),
                quarantine_root="/tmp/not-the-profile-root",
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=0.0,
                poll_s=0.01,
                data_root=str(self.h.data_root),
                profile=M.PROFILE_PRE_RUNTIME_SCRATCH,
            )
        self.assertIn("requires quarantine root", str(ctx2.exception))
        # resolve_profile_roots rejects non-exact CLI overrides.
        with self.assertRaises(M.ToolError):
            M.resolve_profile_roots(
                M.PROFILE_PRE_RUNTIME_SCRATCH, "/tmp", None, require_quarantine=False
            )
        with self.assertRaises(M.ToolError):
            M.resolve_profile_roots(
                M.PROFILE_PRE_RUNTIME_SCRATCH,
                None,
                "/tmp/wrong-q",
                require_quarantine=True,
            )

    def test_absent_lock_permits_present_blocks(self) -> None:
        src, result = self.h.eligible("test-runner")
        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(result["lock_evidence"]["status"], "absent")
        lock = self.h.locks / "test-runner.lock"
        cases: list[tuple[str, Any]] = [
            ("dir", lambda p: p.mkdir()),
            ("file", lambda p: wfile(p, b"lock")),
            ("symlink", lambda p: os.symlink("/nonexistent-lock-target", p)),
            ("special", lambda p: os.mkfifo(p)),
        ]
        for entry_type, maker in cases:
            with self.subTest(entry_type=entry_type):
                if lock.exists() or lock.is_symlink():
                    if lock.is_dir() and not lock.is_symlink():
                        lock.rmdir()
                    else:
                        lock.unlink()
                maker(lock)
                before = list(lock.parent.iterdir()) if lock.parent.exists() else []
                self.h.write_census()
                r = self.h.probe(src)
                self.assertEqual(r["classification"], "protected", r)
                self.assertIn("lock_present", r["reasons"])
                self.assertEqual(r["lock_evidence"]["status"], "present")
                self.assertEqual(r["lock_evidence"]["entry_type"], entry_type)
                self.assertIsNone(r["candidate_token"])
                # No lock mutation.
                self.assertTrue(lock.exists() or lock.is_symlink())
                after = list(lock.parent.iterdir())
                self.assertEqual(sorted(x.name for x in after), sorted(x.name for x in before))
                if lock.is_dir() and not lock.is_symlink():
                    lock.rmdir()
                else:
                    lock.unlink()

        # Unreadable entry: present dir we cannot lstat (chmod 000 parent of lock path).
        # Create lock then make parent unreadable — lexists may still work; use a path
        # that lexists but lstat fails via patched lstat.
        real_lstat = os.lstat
        real_lexists = os.path.lexists

        def fake_lexists(path: str) -> bool:
            if path == str(lock):
                return True
            return real_lexists(path)

        def fake_lstat(path: str, *a: Any, **k: Any) -> os.stat_result:
            if path == str(lock):
                raise OSError("simulated unreadable lock")
            return real_lstat(path, *a, **k)

        with mock.patch.object(os.path, "lexists", side_effect=fake_lexists):
            with mock.patch.object(os, "lstat", side_effect=fake_lstat):
                self.h.write_census()
                r_un = self.h.probe(src)
        self.assertEqual(r_un["classification"], "protected", r_un)
        self.assertIn("lock_present", r_un["reasons"])
        self.assertEqual(r_un["lock_evidence"]["entry_type"], "unreadable")

    def test_protection_fingerprint_and_token_bind_protections(self) -> None:
        src = self.h.put_candidate("verify")
        self.h.write_census(captured_at=iso(self.h.now - timedelta(seconds=2)))
        r1 = self.h.probe(src, started_offset=-30.0)
        self.assertEqual(r1["classification"], "eligible", r1)
        fp1 = r1["protection_fingerprint"]
        token1 = r1["candidate_token"]
        self.h.write_census(captured_at=iso(self.h.now - timedelta(seconds=8)))
        r2 = self.h.probe(src, started_offset=-30.0)
        self.assertEqual(r2["classification"], "eligible", r2)
        self.assertEqual(r2["protection_fingerprint"], fp1)
        self.assertEqual(r2["candidate_token"], token1)

        # Process hit protects and prevents token.
        self.h.processes.append(
            {
                "pid": 55,
                "start_time_ticks": 11,
                "name": "worker",
                "scope_complete": True,
                "references": [{"kind": "cwd", "path": str(src)}],
            }
        )
        self.h.write_census()
        r_proc = self.h.probe(src)
        self.assertEqual(r_proc["classification"], "protected")
        self.assertIn("process_reference_under_candidate", r_proc["reasons"])
        self.assertIsNone(r_proc["candidate_token"])

        # Clear process hit; Paseo agent cwd protects.
        self.h.processes = [
            {
                "pid": 1,
                "start_time_ticks": 10,
                "name": "init",
                "scope_complete": True,
                "references": [],
            }
        ]
        self.h.mark_agent(A, cwd=str(src))
        self.h.write_census()
        r_paseo = self.h.probe(src)
        self.assertEqual(r_paseo["classification"], "protected")
        self.assertIn("agent_cwd_under_candidate", r_paseo["reasons"])
        self.assertIsNone(r_paseo["candidate_token"])

        # Clear Paseo; present lock alters/blocks.
        self.h.paseo = FakePaseo()
        lock = self.h.locks / "verify.lock"
        lock.mkdir()
        self.h.write_census()
        r_lock = self.h.probe(src)
        self.assertEqual(r_lock["classification"], "protected")
        self.assertIn("lock_present", r_lock["reasons"])
        lock.rmdir()

        # Boot id change blocks same-boot proof (no eligible token with old fingerprint).
        self.h.write_census()
        census = json.loads(self.h.census.read_text(encoding="utf-8"))
        census["boot_id"] = "boot-other"
        wjson(self.h.census, census)
        self.h.seed_proc()
        # boot_id mismatch vs live proc boot_id should block.
        r_boot = self.h.probe(src)
        self.assertEqual(r_boot["classification"], "blocked", r_boot)
        self.assertIsNone(r_boot.get("candidate_token") or r_boot["candidate_token"])


if __name__ == "__main__":
    unittest.main()
