#!/usr/bin/env python3
"""Deterministic tests for agent-scratch-cleanup.py. Temporary dirs only."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parent / "agent-scratch-cleanup.py"
spec = importlib.util.spec_from_file_location("agent_scratch_cleanup", SCRIPT)
assert spec and spec.loader
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
G = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BOOT = "boot-test-cleanup-001"


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
        self.children: dict[str, list[str]] = {}
        self.schedules: list[dict[str, Any]] = []
        self.permits: list[dict[str, Any]] = []
        self.terminals: list[dict[str, Any]] = []
        self.page_cap: set[str] = set()

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
            if "ls" in self.page_cap:
                return 0, json.dumps({"pageInfo": {"hasMore": True}}), ""
            if "--label" in a:
                parent = a[a.index("--label") + 1].split("=", 1)[1]
                return ok([{"id": k, "status": "idle"} for k in self.children.get(parent, [])])
            return ok(self.unarchived)
        if a[:2] == ["schedule", "ls"]:
            return ok(self.schedules)
        if a[:2] == ["permit", "ls"]:
            return ok(self.permits)
        if a[:2] == ["terminal", "ls"]:
            return ok(self.terminals)
        return 1, "", f"unknown: {a}"


class H:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="scratch-cleanup-")
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.proc = self.root / "proc"
        self.census = self.root / "run" / "paseo" / "process-census.json"
        self.config = self.root / "config.json"
        for p in (self.runtime / "scratch", self.runtime / "artifacts", self.runtime / "locks", self.proc, self.census.parent):
            p.mkdir(parents=True, exist_ok=True)
        wjson(self.config, {"agents": {"runtimeRoot": str(self.runtime)}})
        self.paseo = FakePaseo()
        self.now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        self.processes: list[dict[str, Any]] = [{"pid": 1, "start_time_ticks": 10, "name": "init"}]

    def close(self) -> None:
        for dp, dns, fns in os.walk(self.root):
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

    def write_census(self, captured_at: str | None = None, complete: bool = True) -> None:
        recs = [
            {
                "pid": p["pid"],
                "start_time_ticks": p["start_time_ticks"],
                "uid": 1000,
                "name": p.get("name", "app"),
                "scope_complete": True,
                "references": p.get("references", []),
            }
            for p in self.processes
        ]
        wjson(
            self.census,
            {
                "schema_version": 1,
                "boot_id": BOOT,
                "captured_at": captured_at or iso(self.now),
                "roots": ["/"],
                "complete": complete,
                "errors": [],
                "processes": recs,
            },
        )
        self.seed_proc()

    def put_scratch(
        self,
        agent_id: str = A,
        *,
        generation: str = G,
        lifecycle: str = "released",
        released_at: str | None = None,
        body: bytes = b"scratch-data",
    ) -> Path:
        sdir = self.runtime / "scratch" / agent_id
        sdir.mkdir(parents=True, exist_ok=True)
        man: dict[str, Any] = {
            "schemaVersion": 1,
            "agentId": agent_id,
            "generation": generation,
            "createdAt": iso(self.now - timedelta(days=2)),
            "lifecycle": lifecycle,
        }
        if lifecycle == "released":
            man["releasedAt"] = released_at or iso(self.now - timedelta(hours=25))
        wjson(sdir / "manifest.json", man)
        wfile(sdir / "tmp.dat", body)
        return sdir

    def put_artifacts(self, agent_id: str = A, content: bytes = b"artifact-bytes") -> Path:
        adir = self.runtime / "artifacts" / agent_id
        adir.mkdir(parents=True, exist_ok=True)
        wjson(
            adir / "manifest.json",
            {
                "schemaVersion": 1,
                "agentId": agent_id,
                "retention": "retained",
                "createdAt": iso(self.now - timedelta(days=2)),
            },
        )
        wfile(adir / "keep.bin", content)
        return adir

    def mark_archived(self, agent_id: str = A, status: str = "closed") -> None:
        self.paseo.inspect[agent_id] = {
            "Id": agent_id,
            "Status": status,
            "Archived": True,
            "ArchivedAt": iso(self.now - timedelta(days=1)),
        }

    def mark_active(self, agent_id: str = A) -> None:
        self.paseo.inspect[agent_id] = {
            "Id": agent_id,
            "Status": "running",
            "Archived": False,
            "ArchivedAt": None,
        }
        self.paseo.unarchived.append({"id": agent_id, "status": "running"})

    def probe(self) -> dict[str, Any]:
        return M.run_probe(
            config_path=str(self.config),
            census_path=str(self.census),
            proc_root=str(self.proc),
            runner=self.paseo,
            now_fn=lambda: self.now,
            wait_s=0.0,
            poll_s=0.01,
            started_at=self.now - timedelta(seconds=1),
        )

    def archive(self, token: str, generation: str = G, agent_id: str = A) -> dict[str, Any]:
        self.write_census(captured_at=iso(self.now + timedelta(seconds=6)))
        return M.run_archive(
            config_path=str(self.config),
            agent_id=agent_id,
            generation=generation,
            candidate_token=token,
            census_path=str(self.census),
            proc_root=str(self.proc),
            runner=self.paseo,
            now_fn=lambda: self.now + timedelta(seconds=5),
            wait_s=0.0,
            poll_s=0.01,
        )

    def eligible_setup(self) -> Path:
        sdir = self.put_scratch()
        self.put_artifacts()
        self.mark_archived()
        self.write_census()
        return sdir


class Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = H()

    def tearDown(self) -> None:
        self.h.close()

    def cand(self) -> dict[str, Any]:
        return self.h.probe()["candidates"][0]

    def test_eligible_closed(self) -> None:
        self.h.eligible_setup()
        self.h.paseo.inspect[A] = {"Id": A, "Status": "closed", "Archived": False, "ArchivedAt": None}
        c = self.cand()
        self.assertEqual(c["classification"], "eligible")
        self.assertTrue(c["candidate_token"])
        self.assertEqual(c["generation"], G)

    def test_protected_and_blocked_matrix(self) -> None:
        cases = [
            ("active_agent", lambda h: (h.put_scratch(), h.mark_active(), h.write_census()), "protected", "agent_not_archived_or_closed"),
            ("grace", lambda h: (h.put_scratch(released_at=iso(h.now - timedelta(hours=1))), h.mark_archived(), h.write_census()), "protected", "release_grace"),
            ("descendant", lambda h: (h.put_scratch(), h.mark_archived(), h.paseo.unarchived.append({"id": B, "status": "idle"}), h.paseo.children.__setitem__(A, [B]), h.write_census()), "protected", "unarchived_descendant"),
            ("schedule", lambda h: (h.put_scratch(), h.mark_archived(), h.paseo.schedules.append({"id": "s", "status": "active", "target": f"agent:{A[:7]}"}), h.write_census()), "protected", "active_schedule"),
            ("permit", lambda h: (h.put_scratch(), h.mark_archived(), h.paseo.permits.append({"id": "r", "agentId": A}), h.write_census()), "protected", "pending_permission"),
            ("terminal", lambda h: (h.put_scratch(), h.mark_archived(), h.paseo.terminals.append({"id": "t", "name": "t", "cwd": str(h.runtime / "scratch" / A)}), h.write_census()), "protected", "terminal_in_scratch"),
            ("process", lambda h: (h.put_scratch(), h.mark_archived(), h.processes.append({"pid": 42, "start_time_ticks": 9, "references": [{"kind": "cwd", "path": str(h.runtime / "scratch" / A / "tmp.dat")}]}), h.write_census()), "protected", "process_in_scratch"),
            ("lock", lambda h: (h.put_scratch(), h.mark_archived(), (h.runtime / "locks" / f"{A}.lock").mkdir(), wjson(h.runtime / "locks" / f"{A}.lock" / "owner.json", {"schemaVersion": 1, "agentId": A, "lockToken": str(uuid.uuid4()), "operation": "prepare", "pid": 1, "acquiredAt": iso(h.now)}), h.write_census()), "protected", "lock_present"),
            ("lifecycle_active", lambda h: (h.put_scratch(lifecycle="active"), h.mark_archived(), h.write_census()), "protected", "lifecycle_active"),
            ("symlink_root", lambda h: (os.symlink(h.root / "e", h.runtime / "scratch" / A) if (h.root / "e").mkdir(exist_ok=True) or True else None, h.mark_archived(), h.write_census()), "blocked", "scratch_is_symlink"),
            ("unknown", lambda h: (h.put_scratch(), h.write_census()), "blocked", "agent_unknown"),
            ("bad_manifest", lambda h: ((h.runtime / "scratch" / A).mkdir(parents=True), wfile(h.runtime / "scratch" / A / "manifest.json", "{bad"), h.mark_archived(), h.write_census()), "blocked", "manifest_unparseable"),
            ("incomplete", lambda h: (h.put_scratch(), h.mark_archived(), h.write_census(complete=False)), "blocked", "snapshot_incomplete"),
            ("stale", lambda h: (h.put_scratch(), h.mark_archived(), h.write_census(captured_at=iso(h.now - timedelta(seconds=120)))), "blocked", "snapshot_stale"),
            ("pre_start", lambda h: (h.put_scratch(), h.mark_archived(), h.write_census(captured_at=iso(h.now - timedelta(seconds=5)))), "blocked", "snapshot_pre_start"),
            ("page_cap", lambda h: (h.put_scratch(), h.mark_archived(), h.paseo.page_cap.add("ls"), h.write_census()), "blocked", "paseo_census"),
        ]
        for name, setup, cls, reason in cases:
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                setup(self.h)
                c = self.cand()
                self.assertEqual(c["classification"], cls, c)
                self.assertTrue(any(reason in r for r in c["reasons"]), c["reasons"])

    def test_pid_mismatch_and_new(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census()
        self.h.processes[0]["start_time_ticks"] = 99999
        self.h.seed_proc()
        self.assertIn("process_pid_mismatch", self.cand()["reasons"])
        self.h.processes[0]["start_time_ticks"] = 10
        self.h.write_census()
        pdir = self.h.proc / "77"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(77, "new", 1))
        self.assertIn("process_new", self.cand()["reasons"])

    def test_size_timeout(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census()
        real, n = time.monotonic, {"n": 0}

        def fake() -> float:
            n["n"] += 1
            return real() + (10_000 if n["n"] > 2 else 0)

        time.monotonic = fake  # type: ignore[assignment]
        try:
            self.assertIn("size_walk_timeout", self.cand()["reasons"])
        finally:
            time.monotonic = real  # type: ignore[assignment]

    def test_token_changes(self) -> None:
        self.h.eligible_setup()
        t1 = self.cand()["candidate_token"]
        wfile(self.h.runtime / "scratch" / A / "tmp.dat", b"changed-bytes-xx")
        self.assertNotEqual(t1, self.cand()["candidate_token"])

    def test_report_only_and_uuid_filter(self) -> None:
        self.h.eligible_setup()
        (self.h.runtime / "quarantine" / "x").mkdir(parents=True)
        wfile(self.h.runtime / "quarantine" / "x" / "q.bin", b"qqqq")
        junk = self.h.runtime / "scratch" / "not-a-uuid"
        junk.mkdir()
        wfile(junk / "x", b"1")
        probe = self.h.probe()
        self.assertIn("root", probe["free_bytes"])
        self.assertGreaterEqual(probe["report_only"]["artifacts_bytes"], 1)
        self.assertGreaterEqual(probe["report_only"]["quarantine_bytes"], 1)
        self.assertEqual([c["agent_id"] for c in probe["candidates"]], [A])

    def test_archive_exact_and_artifacts(self) -> None:
        sdir = self.h.eligible_setup()
        art = self.h.runtime / "artifacts" / A / "keep.bin"
        before = art.read_bytes()
        token = self.cand()["candidate_token"]
        result = self.h.archive(token)
        self.assertEqual(result["status"], "archived")
        self.assertTrue(result["artifacts_preserved"])
        self.assertGreater(result["bytes_removed"], 0)
        self.assertEqual(result["agent_id"], A)
        self.assertEqual(result["generation"], G)
        self.assertFalse(sdir.exists())
        self.assertEqual(art.read_bytes(), before)
        self.assertTrue((self.h.runtime / "artifacts" / A / "manifest.json").exists())
        self.assertFalse((self.h.runtime / "locks" / f"{A}.lock").exists())

    def test_archive_token_mismatch(self) -> None:
        sdir = self.h.eligible_setup()
        with self.assertRaises(M.ToolError):
            self.h.archive("deadbeef" * 8)
        self.assertTrue(sdir.exists())

    def test_archive_refuses_unsafe_contents(self) -> None:
        for kind, mutate, needle in [
            ("symlink", lambda s: os.symlink("/tmp", s / "evil"), "symlink"),
            ("hardlink", lambda s: (wfile(self.h.root / "out.bin", b"shared"), os.link(self.h.root / "out.bin", s / "h.dat")), "hard_link"),
            ("fifo", lambda s: os.mkfifo(s / "p.fifo"), "special_file"),
        ]:
            with self.subTest(kind):
                self.tearDown()
                self.setUp()
                sdir = self.h.eligible_setup()
                mutate(sdir)
                token = self.cand()["candidate_token"]
                with self.assertRaises(M.ToolError) as ctx:
                    self.h.archive(token)
                self.assertIn(needle, str(ctx.exception))
                self.assertTrue(sdir.exists())

    def test_lock_exclusion(self) -> None:
        self.h.eligible_setup()
        token = self.cand()["candidate_token"]
        lock = self.h.runtime / "locks" / f"{A}.lock"
        lock.mkdir()
        wjson(
            lock / "owner.json",
            {
                "schemaVersion": 1,
                "agentId": A,
                "lockToken": str(uuid.uuid4()),
                "operation": "prepare",
                "pid": 1,
                "acquiredAt": iso(self.h.now),
            },
        )
        with self.assertRaises(M.ToolError) as ctx:
            self.h.archive(token)
        self.assertIn("lock", str(ctx.exception).lower())
        self.assertTrue(lock.exists())


if __name__ == "__main__":
    unittest.main()
