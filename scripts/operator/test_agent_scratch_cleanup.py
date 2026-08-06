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
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "agent-scratch-cleanup.py"
spec = importlib.util.spec_from_file_location("agent_scratch_cleanup", SCRIPT)
assert spec and spec.loader
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"
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
        self.schedules: list[dict[str, Any]] = []
        # schedule_id -> identity record (full agentId targets)
        self.schedule_identity: dict[str, dict[str, Any]] = {}
        self.permits: list[dict[str, Any]] = []
        self.terminals: list[dict[str, Any]] = []
        self.page_cap: set[str] = set()
        self.identity_fail: set[str] = set()

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
            # No --label support assumed by the cleanup tool.
            return ok(self.unarchived)
        if a[:2] == ["schedule", "ls"]:
            return ok(self.schedules)
        if a[:2] == ["schedule", "inspect"]:
            sid = a[2]
            if "--identity-only" not in a:
                return 1, "", "identity-only required"
            if sid in self.identity_fail:
                return 1, "", f"identity inspect failed: {sid}"
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
        self.tmp = tempfile.TemporaryDirectory(prefix="scratch-cleanup-")
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.proc = self.root / "proc"
        self.census = self.root / "run" / "paseo" / "process-census.json"
        self.config = self.root / "config.json"
        for p in (
            self.runtime / "scratch",
            self.runtime / "artifacts",
            self.runtime / "locks",
            self.runtime / "quarantine",
            self.proc,
            self.census.parent,
        ):
            p.mkdir(parents=True, exist_ok=True)
        wjson(self.config, {"agents": {"runtimeRoot": str(self.runtime)}})
        self.paseo = FakePaseo()
        self.now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        self.processes: list[dict[str, Any]] = [
            {"pid": 1, "start_time_ticks": 10, "name": "init", "scope_complete": True, "references": []}
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
            # Non-kernel: non-empty cmdline (matches process-census population).
            if p.get("kernel_thread"):
                wfile(pdir / "cmdline", b"")
                # no exe → kernel thread
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
                # Producer omits kernel threads from the snapshot.
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
                "roots": roots if roots is not None else [str(self.runtime)],
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
            "ParentAgentId": None,
        }

    def mark_active(self, agent_id: str = A, parent: str | None = None) -> None:
        self.paseo.inspect[agent_id] = {
            "Id": agent_id,
            "Status": "running",
            "Archived": False,
            "ArchivedAt": None,
            "ParentAgentId": parent,
        }
        self.paseo.unarchived.append({"id": agent_id, "status": "running"})

    def add_active_schedule(self, schedule_id: str, target_agent: str, abbreviated: bool = True) -> None:
        short = target_agent[:7]
        self.paseo.schedules.append(
            {
                "id": schedule_id,
                "status": "active",
                # Display target is abbreviated — tool must not trust this alone.
                "target": f"agent:{short}" if abbreviated else f"agent:{target_agent}",
            }
        )
        self.paseo.schedule_identity[schedule_id] = {
            "id": schedule_id,
            "cadence": {"type": "every", "everyMs": 300_000},
            "target": {"type": "agent", "agentId": target_agent},
            "status": "active",
            "expiresAt": None,
        }

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
        self.h.paseo.inspect[A] = {
            "Id": A,
            "Status": "closed",
            "Archived": False,
            "ArchivedAt": None,
            "ParentAgentId": None,
        }
        c = self.cand()
        self.assertEqual(c["classification"], "eligible")
        self.assertTrue(c["candidate_token"])
        self.assertEqual(c["generation"], G)
        self.assertTrue(c["inventory_fingerprint"])

    def test_protected_and_blocked_matrix(self) -> None:
        cases = [
            (
                "active_agent",
                lambda h: (h.put_scratch(), h.mark_active(), h.write_census()),
                "protected",
                "agent_not_archived_or_closed",
            ),
            (
                "grace",
                lambda h: (
                    h.put_scratch(released_at=iso(h.now - timedelta(hours=1))),
                    h.mark_archived(),
                    h.write_census(),
                ),
                "protected",
                "release_grace",
            ),
            (
                "descendant",
                lambda h: (
                    h.put_scratch(),
                    h.mark_archived(),
                    h.mark_active(B, parent=A),
                    h.write_census(),
                ),
                "protected",
                "unarchived_descendant",
            ),
            (
                "schedule",
                lambda h: (
                    h.put_scratch(),
                    h.mark_archived(),
                    h.add_active_schedule("s1", A),
                    h.write_census(),
                ),
                "protected",
                "active_schedule",
            ),
            (
                "permit",
                lambda h: (
                    h.put_scratch(),
                    h.mark_archived(),
                    h.paseo.permits.append({"id": "r", "agentId": A}),
                    h.write_census(),
                ),
                "protected",
                "pending_permission",
            ),
            (
                "terminal",
                lambda h: (
                    h.put_scratch(),
                    h.mark_archived(),
                    h.paseo.terminals.append(
                        {"id": "t", "name": "t", "cwd": str(h.runtime / "scratch" / A)}
                    ),
                    h.write_census(),
                ),
                "protected",
                "terminal_in_scratch",
            ),
            (
                "process",
                lambda h: (
                    h.put_scratch(),
                    h.mark_archived(),
                    h.processes.append(
                        {
                            "pid": 42,
                            "start_time_ticks": 9,
                            "scope_complete": True,
                            "references": [
                                {
                                    "kind": "cwd",
                                    "path": str(h.runtime / "scratch" / A / "tmp.dat"),
                                }
                            ],
                        }
                    ),
                    h.write_census(),
                ),
                "protected",
                "process_in_scratch",
            ),
            (
                "lock",
                lambda h: (
                    h.put_scratch(),
                    h.mark_archived(),
                    (h.runtime / "locks" / f"{A}.lock").mkdir(),
                    wjson(
                        h.runtime / "locks" / f"{A}.lock" / "owner.json",
                        {
                            "schemaVersion": 1,
                            "agentId": A,
                            "lockToken": str(uuid.uuid4()),
                            "operation": "prepare",
                            "pid": 1,
                            "acquiredAt": iso(h.now),
                        },
                    ),
                    h.write_census(),
                ),
                "protected",
                "lock_present",
            ),
            (
                "lifecycle_active",
                lambda h: (h.put_scratch(lifecycle="active"), h.mark_archived(), h.write_census()),
                "protected",
                "lifecycle_active",
            ),
            (
                "symlink_root",
                lambda h: (
                    os.symlink(h.root / "e", h.runtime / "scratch" / A)
                    if (h.root / "e").mkdir(exist_ok=True) or True
                    else None,
                    h.mark_archived(),
                    h.write_census(),
                ),
                "blocked",
                "scratch_is_symlink",
            ),
            (
                "unknown",
                lambda h: (h.put_scratch(), h.write_census()),
                "blocked",
                "agent_unknown",
            ),
            (
                "bad_manifest",
                lambda h: (
                    (h.runtime / "scratch" / A).mkdir(parents=True),
                    wfile(h.runtime / "scratch" / A / "manifest.json", "{bad"),
                    h.mark_archived(),
                    h.write_census(),
                ),
                "blocked",
                "manifest_unparseable",
            ),
            (
                "incomplete",
                lambda h: (h.put_scratch(), h.mark_archived(), h.write_census(complete=False)),
                "blocked",
                "snapshot_incomplete",
            ),
            (
                "stale",
                lambda h: (
                    h.put_scratch(),
                    h.mark_archived(),
                    h.write_census(captured_at=iso(h.now - timedelta(seconds=120))),
                ),
                "blocked",
                "snapshot_stale",
            ),
            (
                "pre_start",
                lambda h: (
                    h.put_scratch(),
                    h.mark_archived(),
                    h.write_census(captured_at=iso(h.now - timedelta(seconds=5))),
                ),
                "blocked",
                "snapshot_pre_start",
            ),
            (
                "page_cap",
                lambda h: (
                    h.put_scratch(),
                    h.mark_archived(),
                    h.paseo.page_cap.add("ls"),
                    h.write_census(),
                ),
                "blocked",
                "paseo_census",
            ),
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
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")
        self.assertIn("process_new", self.cand()["reasons"])

    def test_malformed_process_refs_block_all(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.processes[0]["references"] = [{"kind": "cwd", "path": "relative/not/abs"}]
        self.h.write_census()
        self.assertIn("snapshot_references_malformed", self.cand()["reasons"])
        self.assertEqual(self.cand()["classification"], "blocked")

        self.tearDown()
        self.setUp()
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.processes[0]["scope_complete"] = False
        self.h.write_census()
        self.assertIn("snapshot_scope_incomplete", self.cand()["reasons"])

    def test_size_timeout_blocks_candidate(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census()
        real_ds = M.dir_size_capped

        def fake_ds(path: str, deadline: float) -> tuple[int | None, str | None]:
            # Only the per-candidate scratch walk times out; aggregates stay ok.
            if str(self.h.runtime / "scratch" / A) in path or path.endswith(f"/scratch/{A}"):
                return None, "size_walk_timeout"
            return real_ds(path, deadline)

        with mock.patch.object(M, "dir_size_capped", side_effect=fake_ds):
            self.assertIn("size_walk_timeout", self.cand()["reasons"])

    def test_aggregate_timeout_unknown_not_zero(self) -> None:
        self.h.eligible_setup()
        wfile(self.h.runtime / "artifacts" / "x.bin", b"abc")
        real_ds = M.dir_size_capped

        def fake_ds(path: str, deadline: float) -> tuple[int | None, str | None]:
            if path.rstrip("/").endswith("/artifacts") or path.rstrip("/").endswith(
                str(self.h.runtime / "artifacts")
            ):
                return None, "size_walk_timeout"
            return real_ds(path, deadline)

        with mock.patch.object(M, "dir_size_capped", side_effect=fake_ds):
            probe = self.h.probe()
        rep = probe["report_only"]
        self.assertIsNone(rep["artifacts_bytes"])
        self.assertEqual(rep["artifacts_status"], "unknown")
        self.assertEqual(rep["artifacts_error"], "size_walk_timeout")
        self.assertTrue(any("report_artifacts" in e for e in rep["errors"]))
        # Must never report a numeric 0 for a timed-out aggregate.
        self.assertNotEqual(rep["artifacts_bytes"], 0)
        # Aggregate failure blocks every scratch candidate for the wake.
        self.assertTrue(probe["candidates"])
        for c in probe["candidates"]:
            self.assertEqual(c["classification"], "blocked")
            self.assertTrue(any("report_artifacts" in r for r in c["reasons"]), c["reasons"])

    def test_token_changes_with_tree(self) -> None:
        self.h.eligible_setup()
        t1 = self.cand()["candidate_token"]
        wfile(self.h.runtime / "scratch" / A / "tmp.dat", b"changed-bytes-xx")
        self.assertNotEqual(t1, self.cand()["candidate_token"])

    def test_unknown_scratch_entry_incomplete(self) -> None:
        self.h.eligible_setup()
        junk = self.h.runtime / "scratch" / "not-a-uuid"
        junk.mkdir()
        wfile(junk / "x", b"1")
        probe = self.h.probe()
        self.assertFalse(probe["scratch_inventory"]["complete"])
        self.assertIn("not-a-uuid", probe["scratch_inventory"]["unknown_entries"])
        self.assertEqual(probe["candidates"][0]["classification"], "blocked")
        self.assertTrue(
            any("unknown_scratch_entry" in r for r in probe["candidates"][0]["reasons"])
        )

    def test_report_only_and_uuid_filter(self) -> None:
        self.h.eligible_setup()
        (self.h.runtime / "quarantine" / "x").mkdir(parents=True)
        wfile(self.h.runtime / "quarantine" / "x" / "q.bin", b"qqqq")
        probe = self.h.probe()
        self.assertIn("root", probe["free_bytes"])
        self.assertGreaterEqual(probe["report_only"]["artifacts_bytes"], 1)
        self.assertGreaterEqual(probe["report_only"]["quarantine_bytes"], 1)
        self.assertEqual(probe["report_only"]["artifacts_status"], "ok")
        self.assertEqual([c["agent_id"] for c in probe["candidates"]], [A])

    def test_schedule_identity_inspection_required(self) -> None:
        """Abbreviated display target alone must not protect/resolve; identity-only does."""
        self.h.put_scratch()
        self.h.mark_archived()
        # Display target looks like agent A, but identity resolves to B → do not protect A.
        self.h.paseo.schedules.append(
            {"id": "s-ambig", "status": "active", "target": f"agent:{A[:7]}"}
        )
        self.h.paseo.schedule_identity["s-ambig"] = {
            "id": "s-ambig",
            "cadence": {"type": "every", "everyMs": 1000},
            "target": {"type": "agent", "agentId": B},
            "status": "active",
            "expiresAt": None,
        }
        self.h.write_census()
        c = self.cand()
        self.assertEqual(c["classification"], "eligible")
        self.assertNotIn("active_schedule", c["reasons"])

        # Identity failure fails closed for all candidates.
        self.tearDown()
        self.setUp()
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.paseo.schedules.append({"id": "s-fail", "status": "active", "target": "agent:1111111"})
        self.h.paseo.identity_fail.add("s-fail")
        self.h.write_census()
        c = self.cand()
        self.assertEqual(c["classification"], "blocked")
        self.assertTrue(any("paseo_census" in r for r in c["reasons"]))

    def test_schedule_identity_protects_resolved_target(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        # Display target abbreviated for B, but identity resolves to A.
        self.h.paseo.schedules.append(
            {"id": "s2", "status": "active", "target": f"agent:{B[:7]}"}
        )
        self.h.paseo.schedule_identity["s2"] = {
            "id": "s2",
            "cadence": {"type": "cron"},
            "target": {"type": "agent", "agentId": A},
            "status": "active",
            "expiresAt": None,
        }
        self.h.write_census()
        c = self.cand()
        self.assertEqual(c["classification"], "protected")
        self.assertIn("active_schedule", c["reasons"])

    def test_active_new_agent_schedule_blocks_all_candidates(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.paseo.schedules.append(
            {"id": "s-new", "status": "active", "target": "new-agent:claude"}
        )
        self.h.paseo.schedule_identity["s-new"] = {
            "id": "s-new",
            "cadence": {"type": "every", "everyMs": 60_000},
            "target": {"type": "new-agent", "config": {"provider": "claude", "cwd": "/ws"}},
            "status": "active",
            "expiresAt": None,
        }
        self.h.write_census()
        c = self.cand()
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("active_new_agent_schedule", c["reasons"])

    def test_malformed_schedule_identity_target_blocks_all(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.paseo.schedules.append(
            {"id": "s-bad", "status": "active", "target": "agent:????"}
        )
        self.h.paseo.schedule_identity["s-bad"] = {
            "id": "s-bad",
            "target": {"type": "mystery"},
            "status": "active",
        }
        self.h.write_census()
        c = self.cand()
        self.assertEqual(c["classification"], "blocked")
        self.assertTrue(
            any(
                r == "active_new_agent_schedule" or r.startswith("schedule_identity_")
                for r in c["reasons"]
            )
        )

    def test_malformed_permit_and_terminal_cwd(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.paseo.permits.append({"id": "p1"})  # missing agentId
        self.h.write_census()
        self.assertTrue(any("paseo_census" in r for r in self.cand()["reasons"]))

        self.tearDown()
        self.setUp()
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.paseo.terminals.append({"id": "t1", "cwd": "-"})
        self.h.write_census()
        self.assertTrue(any("paseo_census" in r for r in self.cand()["reasons"]))

        self.tearDown()
        self.setUp()
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.paseo.terminals.append({"id": "t2", "cwd": "relative/path"})
        self.h.write_census()
        self.assertTrue(any("paseo_census" in r for r in self.cand()["reasons"]))

    def test_malformed_ancestry_blocks(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.paseo.unarchived.append({"id": B, "status": "idle"})
        self.h.paseo.inspect[B] = {
            "Id": B,
            "Status": "idle",
            "Archived": False,
            "ArchivedAt": None,
            "ParentAgentId": "not-a-uuid",
        }
        self.h.write_census()
        self.assertTrue(any("paseo_census" in r for r in self.cand()["reasons"]))

    def test_symlink_lock_parent_fail_closed(self) -> None:
        self.h.eligible_setup()
        token = self.cand()["candidate_token"]
        # Replace locks with a symlink → acquire must fail closed.
        locks = self.h.runtime / "locks"
        for child in locks.iterdir():
            if child.is_dir():
                for f in child.iterdir():
                    f.unlink()
                child.rmdir()
            else:
                child.unlink()
        locks.rmdir()
        target = self.h.root / "evil-locks"
        target.mkdir()
        os.symlink(target, locks)
        with self.assertRaises(M.ToolError) as ctx:
            self.h.archive(token)
        self.assertTrue(
            "symlink" in str(ctx.exception).lower() or "lock" in str(ctx.exception).lower(),
            str(ctx.exception),
        )

    def test_atomic_write_refuses_symlink_parent(self) -> None:
        parent = self.h.root / "aw"
        parent.mkdir()
        link = self.h.root / "link-parent"
        os.symlink(parent, link)
        with self.assertRaises(M.ToolError) as ctx:
            M.atomic_write(str(link / "x.json"), "{}\n")
        self.assertIn("symlink", str(ctx.exception).lower())

    def test_tree_change_between_probes(self) -> None:
        self.h.eligible_setup()
        real = M.tree_inventory
        n = {"c": 0}

        def flaky(root: str, deadline: float) -> tuple[list[dict[str, Any]], list[str]]:
            n["c"] += 1
            recs, errs = real(root, deadline)
            if n["c"] == 1 and recs and not errs:
                # Mutate after first inventory so second probe differs.
                wfile(self.h.runtime / "scratch" / A / "tmp.dat", b"mutated-between-probes")
            return recs, errs

        with mock.patch.object(M, "tree_inventory", side_effect=flaky):
            c = self.cand()
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("tree_changed", c["reasons"])

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
        # No retained tombstone on success
        rel = self.h.runtime / "quarantine" / "released-scratch"
        if rel.exists():
            self.assertEqual(list(rel.iterdir()), [])

    def test_archive_token_mismatch(self) -> None:
        sdir = self.h.eligible_setup()
        with self.assertRaises(M.ToolError):
            self.h.archive("deadbeef" * 8)
        self.assertTrue(sdir.exists())

    def test_archive_refuses_unsafe_contents(self) -> None:
        for kind, mutate, needle in [
            ("symlink", lambda s: os.symlink("/tmp", s / "evil"), "symlink"),
            (
                "hardlink",
                lambda s: (
                    wfile(self.h.root / "out.bin", b"shared"),
                    os.link(self.h.root / "out.bin", s / "h.dat"),
                ),
                "hard_link",
            ),
            ("fifo", lambda s: os.mkfifo(s / "p.fifo"), "special_file"),
        ]:
            with self.subTest(kind):
                self.tearDown()
                self.setUp()
                sdir = self.h.eligible_setup()
                mutate(sdir)
                # Unsafe content blocks eligibility at probe (inventory).
                c = self.cand()
                self.assertEqual(c["classification"], "blocked")
                self.assertTrue(any(needle in r for r in c["reasons"]), c["reasons"])
                self.assertTrue(sdir.exists())

    def test_artifact_change_blocks_archive(self) -> None:
        self.h.eligible_setup()
        token = self.cand()["candidate_token"]
        real_remove = M.remove_tombstone_exact

        def remove_then_mutate(path: str, expected: os.stat_result) -> int:
            n = real_remove(path, expected)
            wfile(self.h.runtime / "artifacts" / A / "keep.bin", b"mutated-artifact!!!")
            return n

        with mock.patch.object(M, "remove_tombstone_exact", side_effect=remove_then_mutate):
            with self.assertRaises(M.ToolError) as ctx:
                self.h.archive(token)
            self.assertIn("artifact", str(ctx.exception).lower())

    def test_recoverable_tombstone_on_removal_failure(self) -> None:
        sdir = self.h.eligible_setup()
        token = self.cand()["candidate_token"]
        real_remove = M.remove_tombstone_exact

        def boom(path: str, expected: os.stat_result) -> int:
            raise M.ToolError("simulated removal failure")

        with mock.patch.object(M, "remove_tombstone_exact", side_effect=boom):
            result = self.h.archive(token)
        self.assertEqual(result["status"], "quarantined_pending_removal")
        self.assertIsNotNone(result.get("tombstone_path"))
        tp = Path(result["tombstone_path"])
        self.assertTrue(tp.exists())
        self.assertTrue(str(tp).startswith(str(self.h.runtime / "quarantine" / "released-scratch")))
        self.assertFalse(sdir.exists())
        # Lock released after outcome recorded
        self.assertFalse((self.h.runtime / "locks" / f"{A}.lock").exists())
        # Scratch free for relaunch; tombstone is under quarantine not scratch/
        self.assertNotEqual(tp.parent, self.h.runtime / "scratch")
        _ = real_remove  # silence unused

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

    def test_chmod_failure_on_lock_not_swallowed(self) -> None:
        self.h.eligible_setup()
        token = self.cand()["candidate_token"]
        real_chmod = os.chmod

        def flaky_chmod(path: str | bytes | os.PathLike[str], mode: int, *a: Any, **k: Any) -> None:
            p = str(path)
            if p.endswith(f"{A}.lock") or p.endswith("/locks"):
                raise OSError("chmod denied in test")
            return real_chmod(path, mode, *a, **k)

        with mock.patch.object(os, "chmod", side_effect=flaky_chmod):
            with self.assertRaises(M.ToolError) as ctx:
                self.h.archive(token)
            self.assertIn("chmod", str(ctx.exception).lower())

    def test_snapshot_roots_must_cover_runtime(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        # Complete snapshot for unrelated roots must block.
        self.h.write_census(roots=["/var/unrelated-census-root"])
        c = self.cand()
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("snapshot_roots_unrelated", c["reasons"])

        self.tearDown()
        self.setUp()
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census(roots=[])
        self.assertIn("snapshot_roots_empty", self.cand()["reasons"])

        self.tearDown()
        self.setUp()
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census(roots=["relative/not/abs", str(self.h.runtime)])
        self.assertIn("snapshot_roots_malformed", self.cand()["reasons"])

        self.tearDown()
        self.setUp()
        self.h.put_scratch()
        self.h.mark_archived()
        # Duplicate roots fail closed.
        self.h.write_census(roots=[str(self.h.runtime), str(self.h.runtime)])
        self.assertIn("snapshot_roots_duplicate", self.cand()["reasons"])

        self.tearDown()
        self.setUp()
        self.h.put_scratch()
        self.h.mark_archived()
        # Symlinked root path is rejected.
        target = self.h.root / "real-root"
        target.mkdir()
        link = self.h.root / "link-root"
        os.symlink(target, link)
        self.h.write_census(roots=[str(link)])
        self.assertIn("snapshot_roots_symlink", self.cand()["reasons"])

    def test_live_pid_map_skips_kernel_threads(self) -> None:
        """Kernel threads omitted by the producer must not require snapshot records."""
        self.h.put_scratch()
        self.h.mark_archived()
        # Live kthread present in fake /proc but absent from snapshot (producer omits).
        self.h.processes.append(
            {
                "pid": 2,
                "start_time_ticks": 1,
                "name": "kthreadd",
                "kernel_thread": True,
            }
        )
        self.h.write_census()
        c = self.cand()
        self.assertEqual(c["classification"], "eligible")
        self.assertNotIn("process_new", c["reasons"])

        # Non-kernel with unreadable identity fails closed.
        self.tearDown()
        self.setUp()
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census()
        pdir = self.h.proc / "88"
        pdir.mkdir()
        wfile(pdir / "cmdline", b"mystery\0")
        # No readable stat → non-kernel unreadable identity.
        self.assertIn("live_process_unreadable", self.cand()["reasons"])

    def test_plain_list_page_cap_blocks(self) -> None:
        """Plain JSON lists of length >= 200 match the worktree probe page-cap rule."""
        for which in ("ls", "schedule", "permit", "terminal"):
            with self.subTest(which=which):
                self.tearDown()
                self.setUp()
                self.h.put_scratch()
                self.h.mark_archived()
                self.h.write_census()
                if which == "ls":
                    self.h.paseo.unarchived = [
                        {"id": str(uuid.uuid4()), "status": "idle"} for _ in range(200)
                    ]
                    # Inspect must succeed for ancestry walk; provide stubs.
                    for item in self.h.paseo.unarchived:
                        self.h.paseo.inspect[item["id"]] = {
                            "Id": item["id"],
                            "Status": "idle",
                            "Archived": False,
                            "ArchivedAt": None,
                            "ParentAgentId": None,
                        }
                elif which == "schedule":
                    self.h.paseo.schedules = [
                        {"id": f"s{i}", "status": "idle"} for i in range(200)
                    ]
                elif which == "permit":
                    self.h.paseo.permits = [
                        {"id": f"p{i}", "agentId": A} for i in range(200)
                    ]
                else:
                    self.h.paseo.terminals = [
                        {
                            "id": f"t{i}",
                            "cwd": str(self.h.runtime / "other"),
                        }
                        for i in range(200)
                    ]
                c = self.cand()
                self.assertEqual(c["classification"], "blocked")
                self.assertTrue(
                    any("page_capped" in r or "paseo_census" in r for r in c["reasons"]),
                    c["reasons"],
                )

    def test_duplicate_snapshot_pid_and_ancestry_cycle(self) -> None:
        self.h.put_scratch()
        self.h.mark_archived()
        # Duplicate PIDs in snapshot processes.
        self.h.processes = [
            {
                "pid": 1,
                "start_time_ticks": 10,
                "name": "init",
                "scope_complete": True,
                "references": [],
            },
            {
                "pid": 1,
                "start_time_ticks": 10,
                "name": "init-dup",
                "scope_complete": True,
                "references": [],
            },
        ]
        self.h.write_census()
        self.assertIn("snapshot_process_duplicate", self.cand()["reasons"])

        # ParentAgentId cycle among unarchived agents blocks.
        self.tearDown()
        self.setUp()
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.paseo.unarchived = [
            {"id": B, "status": "idle"},
            {"id": C, "status": "idle"},
        ]
        self.h.paseo.inspect[B] = {
            "Id": B,
            "Status": "idle",
            "Archived": False,
            "ArchivedAt": None,
            "ParentAgentId": C,
        }
        self.h.paseo.inspect[C] = {
            "Id": C,
            "Status": "idle",
            "Archived": False,
            "ArchivedAt": None,
            "ParentAgentId": B,
        }
        self.h.write_census()
        c = self.cand()
        self.assertEqual(c["classification"], "blocked")
        self.assertTrue(any("paseo_census" in r and "cycle" in r for r in c["reasons"]), c["reasons"])

    def test_default_proof_window_is_75s(self) -> None:
        self.assertEqual(M.SNAPSHOT_WAIT_S, 75)
        self.assertEqual(M.SNAPSHOT_MAX_AGE_S, 45)
        self.assertEqual(
            M.TRANSIENT_PROCESS_REASONS,
            frozenset({"process_new", "process_pid_mismatch", "live_process_race"}),
        )

    def test_transient_process_new_retries_with_newer_snapshot(self) -> None:
        """Exclusive process_new re-waits for a strictly newer snapshot then re-proves."""
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census(captured_at=iso(self.h.now))
        # Live process absent from the first snapshot → exclusive transient.
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
                self.h.write_census(captured_at=iso(self.h.now + timedelta(seconds=1)))

        with mock.patch.object(M.time, "sleep", side_effect=on_sleep):
            probe = M.run_probe(
                config_path=str(self.h.config),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=2.0,
                poll_s=0.05,
                started_at=self.h.now - timedelta(seconds=1),
            )
        self.assertGreaterEqual(sleep_n["n"], 1)
        c = next(x for x in probe["candidates"] if x["agent_id"] == A)
        self.assertEqual(c["classification"], "eligible", c)

    def test_persistent_transient_blocks_with_exact_final_reasons(self) -> None:
        """Churn that never yields a matching newer snapshot blocks with process_new."""
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census(captured_at=iso(self.h.now))
        pdir = self.h.proc / "77"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(77, "new", 1))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")

        with mock.patch.object(M.time, "sleep", return_value=None):
            probe = M.run_probe(
                config_path=str(self.h.config),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=0.15,
                poll_s=0.01,
                started_at=self.h.now - timedelta(seconds=1),
            )
        c = next(x for x in probe["candidates"] if x["agent_id"] == A)
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("process_new", c["reasons"])

    def test_mixed_transient_and_nontransient_blocks_immediately(self) -> None:
        """process_new + non-transient root failure must not enter exclusive retry."""
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census(roots=["/var/unrelated-census-root"], captured_at=iso(self.h.now))
        pdir = self.h.proc / "77"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(77, "new", 1))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")

        sleep_calls: list[float] = []
        with mock.patch.object(M.time, "sleep", side_effect=lambda s: sleep_calls.append(s)):
            probe = M.run_probe(
                config_path=str(self.h.config),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=2.0,
                poll_s=0.05,
                started_at=self.h.now - timedelta(seconds=1),
            )
        c = next(x for x in probe["candidates"] if x["agent_id"] == A)
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("snapshot_roots_unrelated", c["reasons"])
        self.assertIn("process_new", c["reasons"])
        # First snapshot was already acceptable; non-retryable proof → no sleep.
        self.assertEqual(sleep_calls, [])

    def test_process_new_still_blocks_when_wait_zero(self) -> None:
        """Existing zero-wait semantics: process_new blocks without a newer snapshot."""
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census()
        pdir = self.h.proc / "77"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(77, "new", 1))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")
        c = self.cand()
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("process_new", c["reasons"])

    def test_newer_invalid_snapshot_blocks_immediately_not_process_new(self) -> None:
        """M1: after exclusive transient, a newer incomplete snapshot fails closed now.

        Must return snapshot_incomplete immediately — not poll to the deadline and
        not overwrite with the prior process_new reason.
        """
        self.h.put_scratch()
        self.h.mark_archived()
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
                # Strictly newer but incomplete — non-acceptable; block immediately.
                self.h.write_census(
                    captured_at=iso(self.h.now + timedelta(seconds=1)),
                    complete=False,
                )

        with mock.patch.object(M.time, "sleep", side_effect=on_sleep):
            probe = M.run_probe(
                config_path=str(self.h.config),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=2.0,
                poll_s=0.05,
                started_at=self.h.now - timedelta(seconds=1),
            )
        c = next(x for x in probe["candidates"] if x["agent_id"] == A)
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("snapshot_incomplete", c["reasons"])
        self.assertNotIn("process_new", c["reasons"])
        # One sleep to discover the newer invalid snap; no deadline polling.
        self.assertEqual(sleep_n["n"], 1)

    def test_newer_snapshot_without_captured_at_fails_closed(self) -> None:
        """M1: after exclusive transient, unparseable captured_at fails closed immediately."""
        self.h.put_scratch()
        self.h.mark_archived()
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
                # Mutate census so captured_at is missing (cannot establish newer-than).
                raw = json.loads(self.h.census.read_text(encoding="utf-8"))
                del raw["captured_at"]
                self.h.census.write_text(json.dumps(raw), encoding="utf-8")

        with mock.patch.object(M.time, "sleep", side_effect=on_sleep):
            probe = M.run_probe(
                config_path=str(self.h.config),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=2.0,
                poll_s=0.05,
                started_at=self.h.now - timedelta(seconds=1),
            )
        c = next(x for x in probe["candidates"] if x["agent_id"] == A)
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("snapshot_captured_at_missing", c["reasons"])
        self.assertNotIn("process_new", c["reasons"])
        self.assertEqual(sleep_n["n"], 1)

    def test_process_proof_rejects_completion_after_deadline(self) -> None:
        """M2: a proof that would complete at monotonic t=80 with wait_s=75 is rejected.

        Must not authorize cleanup; surface process_proof_timeout (non-transient).
        """
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.write_census(captured_at=iso(self.h.now))

        mono = {"t": 0.0}

        def fake_mono() -> float:
            return mono["t"]

        real_live = M.live_pid_map

        def late_live(
            proc_root: str, *, deadline_mono: float | None = None
        ) -> tuple[dict[int, int | None], list[str]]:
            # Simulate a /proc scan that only finishes after the 75s absolute deadline.
            mono["t"] = 80.0
            return real_live(proc_root, deadline_mono=deadline_mono)

        with mock.patch.object(M.time, "monotonic", side_effect=fake_mono):
            with mock.patch.object(M, "live_pid_map", side_effect=late_live):
                probe = M.run_probe(
                    config_path=str(self.h.config),
                    census_path=str(self.h.census),
                    proc_root=str(self.h.proc),
                    runner=self.h.paseo,
                    now_fn=lambda: self.h.now,
                    wait_s=75.0,
                    poll_s=0.05,
                    started_at=self.h.now - timedelta(seconds=1),
                )
        c = next(x for x in probe["candidates"] if x["agent_id"] == A)
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("process_proof_timeout", c["reasons"])
        self.assertIsNone(c.get("candidate_token"))

    def test_malformed_json_replacement_blocks_immediately_not_process_new(self) -> None:
        """M1: after exclusive transient, malformed JSON replacement fails closed now.

        load_snapshot must not collapse parse failure into snapshot_missing; wait must
        return snapshot_malformed immediately and not retain process_new through the
        deadline.
        """
        self.h.put_scratch()
        self.h.mark_archived()
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
                # Replace with unparseable JSON (malformed load outcome).
                self.h.census.write_text("{not-valid-json", encoding="utf-8")

        with mock.patch.object(M.time, "sleep", side_effect=on_sleep):
            probe = M.run_probe(
                config_path=str(self.h.config),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=2.0,
                poll_s=0.05,
                started_at=self.h.now - timedelta(seconds=1),
            )
        c = next(x for x in probe["candidates"] if x["agent_id"] == A)
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("snapshot_malformed", c["reasons"])
        self.assertNotIn("process_new", c["reasons"])
        self.assertNotIn("snapshot_missing", c["reasons"])
        # One sleep to discover the malformed replacement; no deadline polling.
        self.assertEqual(sleep_n["n"], 1)

    def test_non_object_snapshot_is_malformed_not_missing(self) -> None:
        """M1: JSON array/non-object root is snapshot_malformed (nontransient)."""
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.seed_proc()
        self.h.census.write_text("[]\n", encoding="utf-8")
        c = self.cand()
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("snapshot_malformed", c["reasons"])
        self.assertNotIn("snapshot_missing", c["reasons"])

    def test_load_snapshot_outcomes_distinguish_missing_malformed_unreadable(self) -> None:
        """M1: explicit load tags for absence / parse failure / read error."""
        missing_path = str(self.h.root / "no-such-census.json")
        snap, err = M.load_snapshot(missing_path)
        self.assertIsNone(snap)
        self.assertEqual(err, "snapshot_missing")

        bad = self.h.root / "bad.json"
        bad.write_text("{broken", encoding="utf-8")
        snap, err = M.load_snapshot(str(bad))
        self.assertIsNone(snap)
        self.assertEqual(err, "snapshot_malformed")

        non_obj = self.h.root / "arr.json"
        non_obj.write_text("null", encoding="utf-8")
        snap, err = M.load_snapshot(str(non_obj))
        self.assertIsNone(snap)
        self.assertEqual(err, "snapshot_malformed")

        # Present directory must be unreadable, never collapsed to missing via isfile.
        as_dir = self.h.root / "census-as-dir"
        as_dir.mkdir()
        snap, err = M.load_snapshot(str(as_dir))
        self.assertIsNone(snap)
        self.assertEqual(err, "snapshot_unreadable")

        # Permission denied → unreadable (skip if we cannot drop mode as this user).
        denied = self.h.root / "denied.json"
        denied.write_text('{"ok": true}', encoding="utf-8")
        os.chmod(denied, 0o000)
        try:
            snap, err = M.load_snapshot(str(denied))
            if err == "snapshot_unreadable":
                self.assertIsNone(snap)
            else:
                # Some environments still allow root/owner read; require a real tag.
                self.assertIn(err, (None, "snapshot_malformed", "snapshot_unreadable"))
        finally:
            os.chmod(denied, 0o644)

        self.h.write_census()
        snap, err = M.load_snapshot(str(self.h.census))
        self.assertIsInstance(snap, dict)
        self.assertIsNone(err)

    def test_present_directory_census_is_unreadable_immediately(self) -> None:
        """M1: census path that is a directory → snapshot_unreadable, no poll."""
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.seed_proc()
        if self.h.census.exists() and not self.h.census.is_dir():
            self.h.census.unlink()
        # Present directory at census path (IsADirectoryError on open).
        self.h.census.mkdir(parents=True, exist_ok=True)

        sleep_calls: list[float] = []
        with mock.patch.object(M.time, "sleep", side_effect=lambda s: sleep_calls.append(s)):
            probe = M.run_probe(
                config_path=str(self.h.config),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=2.0,
                poll_s=0.05,
                started_at=self.h.now - timedelta(seconds=1),
            )
        c = next(x for x in probe["candidates"] if x["agent_id"] == A)
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("snapshot_unreadable", c["reasons"])
        self.assertNotIn("snapshot_missing", c["reasons"])
        self.assertEqual(sleep_calls, [])

    def test_directory_replacement_after_transient_not_process_new(self) -> None:
        """M1: after exclusive transient, directory at census path fails closed now.

        Must return snapshot_unreadable immediately and must not retain process_new
        through deadline polling (pre-open isfile would have mis-tagged as missing).
        """
        self.h.put_scratch()
        self.h.mark_archived()
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
                # Replace census file with a present directory.
                self.h.census.unlink()
                self.h.census.mkdir()

        with mock.patch.object(M.time, "sleep", side_effect=on_sleep):
            probe = M.run_probe(
                config_path=str(self.h.config),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=2.0,
                poll_s=0.05,
                started_at=self.h.now - timedelta(seconds=1),
            )
        c = next(x for x in probe["candidates"] if x["agent_id"] == A)
        self.assertEqual(c["classification"], "blocked")
        self.assertIn("snapshot_unreadable", c["reasons"])
        self.assertNotIn("process_new", c["reasons"])
        self.assertNotIn("snapshot_missing", c["reasons"])
        # One sleep to discover the directory replacement; no deadline polling.
        self.assertEqual(sleep_n["n"], 1)

    def test_true_absence_still_polls_and_recovers(self) -> None:
        """M1: true path absence stays snapshot_missing (pollable) and can recover."""
        self.h.put_scratch()
        self.h.mark_archived()
        self.h.seed_proc()
        if self.h.census.exists():
            self.h.census.unlink()

        sleep_n = {"n": 0}

        def on_sleep(_s: float) -> None:
            sleep_n["n"] += 1
            if sleep_n["n"] == 1:
                # Atomic-replace style: path reappears as a good census file.
                self.h.write_census(captured_at=iso(self.h.now))

        with mock.patch.object(M.time, "sleep", side_effect=on_sleep):
            probe = M.run_probe(
                config_path=str(self.h.config),
                census_path=str(self.h.census),
                proc_root=str(self.h.proc),
                runner=self.h.paseo,
                now_fn=lambda: self.h.now,
                wait_s=2.0,
                poll_s=0.05,
                started_at=self.h.now - timedelta(seconds=1),
            )
        self.assertGreaterEqual(sleep_n["n"], 1)
        c = next(x for x in probe["candidates"] if x["agent_id"] == A)
        self.assertEqual(c["classification"], "eligible", c)
        self.assertNotIn("snapshot_unreadable", c["reasons"])
        self.assertNotIn("snapshot_missing", c["reasons"])

    def test_wait_for_snapshot_setup_delay_does_not_extend_absolute_deadline(self) -> None:
        """M2: 10s setup with wait_s=75 must finish by mono 75, not 85.

        wait_for_snapshot must use the original absolute deadline for checks/sleeps
        rather than rebasing time.monotonic()+remaining after boot-id work.
        """
        # No usable census → poll until absolute deadline.
        if self.h.census.exists():
            self.h.census.unlink()

        mono = {"t": 0.0}

        def fake_mono() -> float:
            return mono["t"]

        def slow_boot_id(_proc_root: str) -> str:
            # Simulated setup delay after the caller already fixed the absolute deadline.
            mono["t"] += 10.0
            return BOOT

        sleep_calls: list[float] = []

        def fake_sleep(s: float) -> None:
            sleep_calls.append(s)
            mono["t"] += s

        with mock.patch.object(M.time, "monotonic", side_effect=fake_mono):
            with mock.patch.object(M, "read_boot_id", side_effect=slow_boot_id):
                with mock.patch.object(M.time, "sleep", side_effect=fake_sleep):
                    _snap, reasons = M.wait_for_snapshot(
                        str(self.h.census),
                        proc_root=str(self.h.proc),
                        started_at=self.h.now - timedelta(seconds=1),
                        now_fn=lambda: self.h.now,
                        wait_s=75.0,
                        poll_s=30.0,
                        deadline_mono=75.0,
                    )
        self.assertEqual(reasons, ["snapshot_missing"])
        # Original absolute deadline is 75; setup cost 10 must not yield finish at 85.
        self.assertLessEqual(mono["t"], 75.0)
        self.assertGreater(mono["t"], 10.0)
        # Final sleep must be clamped to remaining after setup (≤65), not full poll.
        self.assertTrue(sleep_calls)
        self.assertLessEqual(sum(sleep_calls), 65.0 + 1e-9)

    def test_wait_for_process_proof_passes_original_absolute_deadline(self) -> None:
        """M2: production helper always binds wait_for_snapshot to original deadline."""
        # Missing census forces polling; 10s boot-id setup must not extend past 75.
        if self.h.census.exists():
            self.h.census.unlink()

        mono = {"t": 0.0}
        seen_deadlines: list[float | None] = []

        def fake_mono() -> float:
            return mono["t"]

        real_wait = M.wait_for_snapshot

        def tracking_wait(*args: Any, **kwargs: Any) -> tuple[dict[str, Any] | None, list[str]]:
            seen_deadlines.append(kwargs.get("deadline_mono"))
            return real_wait(*args, **kwargs)

        def slow_boot_id(_proc_root: str) -> str:
            mono["t"] += 10.0
            return BOOT

        def fake_sleep(s: float) -> None:
            mono["t"] += s

        with mock.patch.object(M.time, "monotonic", side_effect=fake_mono):
            with mock.patch.object(M, "read_boot_id", side_effect=slow_boot_id):
                with mock.patch.object(M, "wait_for_snapshot", side_effect=tracking_wait):
                    with mock.patch.object(M.time, "sleep", side_effect=fake_sleep):
                        ok, reasons, _by_pid, _snap = M.wait_for_process_proof(
                            str(self.h.census),
                            str(self.h.proc),
                            str(self.h.runtime / "scratch"),
                            started_at=self.h.now - timedelta(seconds=1),
                            now_fn=lambda: self.h.now,
                            wait_s=75.0,
                            poll_s=30.0,
                        )
        self.assertFalse(ok)
        self.assertEqual(reasons, ["snapshot_missing"])
        self.assertTrue(seen_deadlines)
        # Production positive window must pass the original absolute deadline (75.0).
        self.assertEqual(seen_deadlines[0], 75.0)
        self.assertLessEqual(mono["t"], 75.0)


if __name__ == "__main__":
    unittest.main()
