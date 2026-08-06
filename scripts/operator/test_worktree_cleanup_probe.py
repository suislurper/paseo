#!/usr/bin/env python3
"""Focused tests for worktree-cleanup-probe.py. Fake proc/snapshot/commands only."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "worktree-cleanup-probe.py"
spec = importlib.util.spec_from_file_location("worktree_cleanup_probe", SCRIPT)
assert spec and spec.loader
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

BOOT = "boot-worktree-probe-001"
PIN_A = "11111111-1111-4111-8111-111111111111"
PIN_B = "22222222-2222-4222-8222-222222222222"


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


class Harness:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="wt-cleanup-probe-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.managed = self.root / "worktrees"
        self.proc = self.root / "proc"
        self.census = self.root / "run" / "paseo" / "process-census.json"
        self.policy = self.root / "WORKTREE_CLEANUP_POLICY.md"
        self.candidate = self.managed / "agent" / "feat-branch"
        self.git_dir = self.repo / ".git" / "worktrees" / "feat-branch"
        for p in (self.repo, self.managed, self.proc, self.census.parent, self.candidate, self.git_dir):
            p.mkdir(parents=True, exist_ok=True)
        self.policy.write_text(
            textwrap.dedent(
                f"""\
                # Policy

                ## Manual pins

                - `{PIN_A}`
                - `{PIN_B}`

                ## Other

                more text
                """
            ),
            encoding="utf-8",
        )
        self.now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
        self.started = self.now - timedelta(seconds=5)
        self.processes: list[dict[str, Any]] = [
            {
                "pid": 1,
                "start_time_ticks": 10,
                "name": "init",
                "uid": 0,
                "scope_complete": True,
                "references": [],
            }
        ]
        self.head = "a" * 40
        self.remote_sha = "b" * 40
        self.branch = "feat/cleanup-probe"

    def close(self) -> None:
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
        *,
        captured_at: str | None = None,
        complete: bool = True,
        boot_id: str = BOOT,
        roots: list[str] | None = None,
        processes: list[dict[str, Any]] | None = None,
        schema_version: int = 1,
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
                    "uid": p.get("uid", 1000),
                    "name": p.get("name", "app"),
                    "scope_complete": p.get("scope_complete", True),
                    "references": p.get("references", []),
                }
            )
        default_roots = [str(self.managed), str(self.repo / ".git")]
        wjson(
            self.census,
            {
                "schema_version": schema_version,
                "boot_id": boot_id,
                "captured_at": captured_at or iso(self.now),
                "roots": roots if roots is not None else default_roots,
                "complete": complete,
                "errors": [],
                "processes": recs,
            },
        )
        self.seed_proc()

    def worktree_item(self) -> dict[str, Any]:
        return {
            "worktree": str(self.candidate),
            "HEAD": self.head,
            "branch": f"refs/heads/{self.branch}",
        }

    def fake_checked(self, argv: list[str], *, cwd: str | None = None, timeout: float = 30) -> str:
        if argv[:3] == ["git", "-C", str(self.repo)] and argv[3:5] == ["ls-remote", "--symref"]:
            return f"ref: refs/heads/main\tHEAD\n{self.remote_sha}\tHEAD\n"
        if argv[:3] == ["git", "-C", str(self.repo)] and argv[3:6] == ["worktree", "list", "--porcelain"]:
            return (
                f"worktree {self.repo}\nHEAD {self.remote_sha}\nbranch refs/heads/main\n\n"
                f"worktree {self.candidate}\nHEAD {self.head}\nbranch refs/heads/{self.branch}\n\n"
            )
        if argv[:2] == ["git", "-C"] and len(argv) >= 4:
            path = argv[2]
            rest = argv[3:]
            if rest == ["rev-parse", "--show-toplevel"]:
                return path + "\n"
            if rest == ["rev-parse", "--absolute-git-dir"]:
                return str(self.git_dir) + "\n"
            if rest == ["rev-parse", "--path-format=absolute", "--git-common-dir"]:
                return str(self.repo / ".git") + "\n"
            if rest == ["symbolic-ref", "-q", "--short", "HEAD"]:
                return self.branch + "\n"
            if rest == ["rev-parse", "HEAD"]:
                return self.head + "\n"
            if rest[:1] == ["rev-list"] and "--count" in rest:
                return "0\n"
        if argv[:1] == ["paseo"] and "ls" in argv and "--global" in argv:
            return "[]\n"
        if argv[:1] == ["paseo"] and argv[1:3] == ["schedule", "ls"]:
            return "[]\n"
        if argv[:1] == ["paseo"] and argv[1:3] == ["terminal", "ls"]:
            return "[]\n"
        if argv[:1] == ["paseo"] and argv[1:3] == ["permit", "ls"]:
            return "[]\n"
        if argv[:1] == ["paseo"] and argv[1] == "inspect":
            aid = argv[2]
            return json.dumps({"id": aid, "cwd": str(self.repo), "status": "closed"}) + "\n"
        raise AssertionError(f"unexpected checked command: {argv}")

    def fake_run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float = 30,
        text: bool = True,
    ) -> Any:
        class R:
            def __init__(self, code: int = 0, out: Any = "", err: Any = "") -> None:
                self.returncode = code
                self.stdout = out
                self.stderr = err

        if argv and argv[0] == "du":
            return R(0, "4096\t" + argv[-1] + "\n", "")
        if argv and argv[0] == "lslocks":
            return R(0, json.dumps({"locks": []}), "")
        if argv[:2] == ["git", "-C"] and "status" in argv:
            empty = b"" if not text else ""
            return R(0, empty, b"" if not text else "")
        if argv[:2] == ["git", "-C"] and "cat-file" in argv:
            return R(0, "", "")
        if argv[:2] == ["git", "-C"] and "merge-base" in argv:
            return R(0, "", "")
        # Fall through to checked-style git/paseo for text commands used via run.
        try:
            out = self.fake_checked(argv, cwd=cwd, timeout=timeout)
            return R(0, out if text else out.encode(), "")
        except AssertionError:
            return R(1, "", f"unexpected:{argv}")


class ProcessOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def tearDown(self) -> None:
        self.h.close()

    def test_owner_cwd_and_open_fd_under_checkout(self) -> None:
        cand = str(self.h.candidate)
        git_dir = str(self.h.git_dir)
        by_pid = {
            42: {
                "pid": 42,
                "uid": 1000,
                "name": "node",
                "references": [
                    {"kind": "cwd", "path": cand},
                    {"kind": "open_fd", "path": f"{cand}/src/main.ts"},
                    {"kind": "exe", "path": "/usr/bin/node"},
                ],
            }
        }
        owners = M.process_owners_from_snapshot(cand, git_dir, by_pid)
        kinds = {(o["kind"], o["path"]) for o in owners}
        self.assertIn(("cwd", cand), kinds)
        self.assertIn(("open_fd", f"{cand}/src/main.ts"), kinds)
        self.assertNotIn(("exe", "/usr/bin/node"), kinds)
        for o in owners:
            self.assertEqual(o["pid"], 42)
            self.assertEqual(o["uid"], 1000)
            self.assertEqual(o["name"], "node")
            self.assertIn(o["kind"], M.REF_KINDS)
            self.assertTrue(os.path.isabs(o["path"]))
            # No argv/env keys
            self.assertNotIn("argv", o)
            self.assertNotIn("env", o)
            self.assertNotIn("cmdline", o)

    def test_owner_under_exact_git_dir(self) -> None:
        cand = str(self.h.candidate)
        git_dir = str(self.h.git_dir)
        by_pid = {
            7: {
                "pid": 7,
                "uid": 1000,
                "name": "git",
                "references": [
                    {"kind": "cwd", "path": f"{git_dir}/index.lock"},
                    {"kind": "interpreter_script", "path": f"{git_dir}/hooks/pre-commit"},
                ],
            }
        }
        owners = M.process_owners_from_snapshot(cand, git_dir, by_pid)
        self.assertEqual(len(owners), 2)
        self.assertTrue(all(o["pid"] == 7 for o in owners))

    def test_owner_open_fd_and_cwd_under_git_worktrees_name(self) -> None:
        """Protect exact candidate git-dir under .git/worktrees/<name>."""
        cand = str(self.h.candidate)
        git_dir = str(self.h.git_dir)
        self.assertIn("/.git/worktrees/", git_dir.replace("\\", "/"))
        by_pid = {
            11: {
                "pid": 11,
                "uid": 1000,
                "name": "git",
                "references": [
                    {"kind": "cwd", "path": git_dir},
                    {"kind": "open_fd", "path": f"{git_dir}/commondir"},
                    {"kind": "open_fd", "path": f"{git_dir}/logs/HEAD"},
                ],
            }
        }
        owners = M.process_owners_from_snapshot(cand, git_dir, by_pid)
        kinds = {(o["kind"], o["path"]) for o in owners}
        self.assertIn(("cwd", git_dir), kinds)
        self.assertIn(("open_fd", f"{git_dir}/commondir"), kinds)
        self.assertIn(("open_fd", f"{git_dir}/logs/HEAD"), kinds)
        self.assertEqual(len(owners), 3)

    def test_no_owner_when_refs_outside(self) -> None:
        cand = str(self.h.candidate)
        by_pid = {
            9: {
                "pid": 9,
                "uid": 1000,
                "name": "sleep",
                "references": [{"kind": "cwd", "path": "/tmp/unrelated"}],
            }
        }
        self.assertEqual(M.process_owners_from_snapshot(cand, str(self.h.git_dir), by_pid), [])


class CensusConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def tearDown(self) -> None:
        self.h.close()

    def _collect(self, **kwargs: Any) -> tuple[bool, list[str], dict[int, dict[str, Any]], dict[str, Any]]:
        self.h.write_census(**{k: v for k, v in kwargs.items() if k in {
            "captured_at", "complete", "boot_id", "roots", "processes", "schema_version"
        }})
        return M.collect_process_census(
            census_path=str(self.h.census),
            proc_root=str(self.h.proc),
            managed_root=str(self.h.managed),
            common_git_dir=str(self.h.repo / ".git"),
            started_at=self.h.started,
            now_fn=lambda: self.h.now,
            wait_s=0.0,
            poll_s=0.0,
        )

    def test_complete_no_owner(self) -> None:
        ok, reasons, by_pid, summary = self._collect()
        self.assertTrue(ok)
        self.assertEqual(reasons, [])
        self.assertIn(1, by_pid)
        self.assertEqual(summary["status"], "ok")
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["boot_id"], BOOT)
        self.assertEqual(summary["captured_at"], iso(self.h.now))

    def test_complete_with_owner_reference(self) -> None:
        cand = str(self.h.candidate)
        self.h.processes.append(
            {
                "pid": 55,
                "start_time_ticks": 99,
                "name": "python3",
                "uid": 1000,
                "scope_complete": True,
                "references": [{"kind": "cwd", "path": cand}],
            }
        )
        ok, reasons, by_pid, _ = self._collect()
        self.assertTrue(ok)
        self.assertEqual(reasons, [])
        owners = M.process_owners_from_snapshot(cand, str(self.h.git_dir), by_pid)
        self.assertEqual(owners[0]["kind"], "cwd")
        self.assertEqual(owners[0]["path"], cand)

    def test_stale_snapshot(self) -> None:
        ok, reasons, _, summary = self._collect(
            captured_at=iso(self.h.now - timedelta(seconds=120))
        )
        self.assertFalse(ok)
        self.assertIn("snapshot_stale", reasons)
        self.assertFalse(summary["complete"])

    def test_incomplete_snapshot(self) -> None:
        ok, reasons, _, _ = self._collect(complete=False)
        self.assertFalse(ok)
        self.assertIn("snapshot_incomplete", reasons)

    def test_pre_start_snapshot(self) -> None:
        ok, reasons, _, _ = self._collect(
            captured_at=iso(self.h.started - timedelta(seconds=1))
        )
        self.assertFalse(ok)
        self.assertIn("snapshot_pre_start", reasons)

    def test_other_boot(self) -> None:
        ok, reasons, _, _ = self._collect(boot_id="other-boot")
        self.assertFalse(ok)
        self.assertIn("snapshot_boot_mismatch", reasons)

    def test_unrelated_root(self) -> None:
        ok, reasons, _, _ = self._collect(roots=["/var/empty-unrelated"])
        self.assertFalse(ok)
        self.assertIn("snapshot_roots_unrelated", reasons)

    def test_malformed_references(self) -> None:
        self.h.processes = [
            {
                "pid": 1,
                "start_time_ticks": 10,
                "name": "init",
                "scope_complete": True,
                "references": [{"kind": "cwd", "path": "relative-not-abs"}],
            }
        ]
        ok, reasons, _, _ = self._collect()
        self.assertFalse(ok)
        self.assertIn("snapshot_references_malformed", reasons)

    def test_malformed_kind(self) -> None:
        self.h.processes = [
            {
                "pid": 1,
                "start_time_ticks": 10,
                "name": "init",
                "scope_complete": True,
                "references": [{"kind": "argv", "path": "/tmp/x"}],
            }
        ]
        ok, reasons, _, _ = self._collect()
        self.assertFalse(ok)
        self.assertIn("snapshot_references_malformed", reasons)

    def test_new_process(self) -> None:
        # Live proc has pid 2 not in snapshot.
        self.h.write_census()
        pdir = self.h.proc / "2"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(2, "new", 50))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")
        ok, reasons, _, _ = M.collect_process_census(
            census_path=str(self.h.census),
            proc_root=str(self.h.proc),
            managed_root=str(self.h.managed),
            common_git_dir=str(self.h.repo / ".git"),
            started_at=self.h.started,
            now_fn=lambda: self.h.now,
            wait_s=0.0,
            poll_s=0.0,
        )
        self.assertFalse(ok)
        self.assertIn("process_new", reasons)

    def test_unreadable_live_process(self) -> None:
        self.h.write_census()
        pdir = self.h.proc / "88"
        pdir.mkdir()
        wfile(pdir / "cmdline", b"mystery\0")
        # No readable stat → fail closed.
        ok, reasons, _, _ = M.collect_process_census(
            census_path=str(self.h.census),
            proc_root=str(self.h.proc),
            managed_root=str(self.h.managed),
            common_git_dir=str(self.h.repo / ".git"),
            started_at=self.h.started,
            now_fn=lambda: self.h.now,
            wait_s=0.0,
            poll_s=0.0,
        )
        self.assertFalse(ok)
        self.assertIn("live_process_unreadable", reasons)

    def test_missing_common_git_root_coverage_blocks(self) -> None:
        # Managed covered, common git directory not covered → block all.
        ok, reasons, _, _ = self._collect(roots=[str(self.h.managed)])
        self.assertFalse(ok)
        self.assertIn("snapshot_roots_unrelated", reasons)

    def test_missing_managed_root_coverage_blocks(self) -> None:
        ok, reasons, _, _ = self._collect(roots=[str(self.h.repo / ".git")])
        self.assertFalse(ok)
        self.assertIn("snapshot_roots_unrelated", reasons)

    def test_snapshot_roots_symlink(self) -> None:
        target = self.h.root / "real-root"
        target.mkdir()
        link = self.h.root / "link-root"
        os.symlink(target, link)
        ok, reasons, _, _ = self._collect(roots=[str(link)])
        self.assertFalse(ok)
        self.assertIn("snapshot_roots_symlink", reasons)

    def test_snapshot_file_symlink_blocks(self) -> None:
        real = self.h.root / "real-census.json"
        self.h.write_census()
        self.h.census.replace(real)
        link = self.h.root / "census-link.json"
        os.symlink(real, link)
        ok, reasons, _, _ = M.collect_process_census(
            census_path=str(link),
            proc_root=str(self.h.proc),
            managed_root=str(self.h.managed),
            common_git_dir=str(self.h.repo / ".git"),
            started_at=self.h.started,
            now_fn=lambda: self.h.now,
            wait_s=0.0,
            poll_s=0.0,
        )
        self.assertFalse(ok)
        self.assertTrue(any("symlink" in reason for reason in reasons))

    def test_scope_incomplete_blocks(self) -> None:
        self.h.processes = [
            {
                "pid": 1,
                "start_time_ticks": 10,
                "name": "init",
                "scope_complete": False,
                "references": [],
            }
        ]
        ok, reasons, _, _ = self._collect()
        self.assertFalse(ok)
        self.assertIn("snapshot_scope_incomplete", reasons)

    def test_duplicate_pid_blocks(self) -> None:
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
        ok, reasons, _, _ = self._collect()
        self.assertFalse(ok)
        self.assertIn("snapshot_process_duplicate", reasons)


class InspectAndSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def tearDown(self) -> None:
        self.h.close()

    def test_process_incompleteness_blocks_all_candidates(self) -> None:
        census = {
            "complete": True,
            "errors": [],
            "agents": [],
            "pins": [],
            "active_schedules": [],
            "global_schedule_blocks": [],
            "terminals": [],
            "pending_permissions": [],
        }
        remote = {"remote": "origin", "ref": "refs/heads/main", "sha": self.h.remote_sha}
        with mock.patch.object(M, "run", side_effect=self.h.fake_run), mock.patch.object(
            M, "checked", side_effect=self.h.fake_checked
        ), mock.patch.object(M, "size_bytes", return_value=(4096, None)):
            result = M.inspect_candidate(
                self.h.worktree_item(),
                repo=str(self.h.repo),
                managed_root=str(self.h.managed),
                remote=remote,
                census=census,
                process_ok=False,
                process_reasons=["snapshot_stale"],
                by_pid={},
                locks=[],
                lock_error=None,
            )
        self.assertEqual(result["local_gate"], "blocked")
        self.assertTrue(
            any(b.startswith("process-census-incomplete:snapshot_stale") for b in result["blockers"]),
            result["blockers"],
        )
        self.assertEqual(result["processes"], [])

    def test_active_new_agent_schedule_blocks_all_candidates(self) -> None:
        census = {
            "complete": True,
            "errors": [],
            "agents": [],
            "pins": [],
            "active_schedules": [
                {
                    "id": "s-new",
                    "status": "active",
                    "target": {"type": "new-agent", "config": {"provider": "claude"}},
                }
            ],
            "global_schedule_blocks": ["active_new_agent_schedule"],
            "terminals": [],
            "pending_permissions": [],
        }
        remote = {"remote": "origin", "ref": "refs/heads/main", "sha": self.h.remote_sha}
        with mock.patch.object(M, "run", side_effect=self.h.fake_run), mock.patch.object(
            M, "checked", side_effect=self.h.fake_checked
        ), mock.patch.object(M, "size_bytes", return_value=(4096, None)):
            result = M.inspect_candidate(
                self.h.worktree_item(),
                repo=str(self.h.repo),
                managed_root=str(self.h.managed),
                remote=remote,
                census=census,
                process_ok=True,
                process_reasons=[],
                by_pid={},
                locks=[],
                lock_error=None,
            )
        self.assertEqual(result["local_gate"], "blocked")
        self.assertIn("active_new_agent_schedule", result["blockers"])

    def test_open_fd_owner_blocks_candidate(self) -> None:
        cand = str(self.h.candidate)
        census = {
            "complete": True,
            "errors": [],
            "agents": [],
            "pins": [],
            "active_schedules": [],
            "terminals": [],
            "pending_permissions": [],
        }
        remote = {"remote": "origin", "ref": "refs/heads/main", "sha": self.h.remote_sha}
        by_pid = {
            77: {
                "pid": 77,
                "uid": 1000,
                "name": "node",
                "references": [
                    {"kind": "open_fd", "path": f"{cand}/package.json"},
                ],
            }
        }
        with mock.patch.object(M, "run", side_effect=self.h.fake_run), mock.patch.object(
            M, "checked", side_effect=self.h.fake_checked
        ), mock.patch.object(M, "size_bytes", return_value=(4096, None)):
            result = M.inspect_candidate(
                self.h.worktree_item(),
                repo=str(self.h.repo),
                managed_root=str(self.h.managed),
                remote=remote,
                census=census,
                process_ok=True,
                process_reasons=[],
                by_pid=by_pid,
                locks=[],
                lock_error=None,
            )
        self.assertEqual(result["local_gate"], "blocked")
        self.assertTrue(any(b.startswith("processes:77") for b in result["blockers"]))
        self.assertEqual(result["processes"][0]["kind"], "open_fd")
        self.assertEqual(result["processes"][0]["path"], f"{cand}/package.json")

    def test_no_owner_process_pass_when_other_gates_clear(self) -> None:
        census = {
            "complete": True,
            "errors": [],
            "agents": [],
            "pins": [],
            "active_schedules": [],
            "terminals": [],
            "pending_permissions": [],
        }
        remote = {"remote": "origin", "ref": "refs/heads/main", "sha": self.h.remote_sha}
        by_pid = {
            1: {
                "pid": 1,
                "uid": 0,
                "name": "init",
                "references": [],
            }
        }
        with mock.patch.object(M, "run", side_effect=self.h.fake_run), mock.patch.object(
            M, "checked", side_effect=self.h.fake_checked
        ), mock.patch.object(M, "size_bytes", return_value=(4096, None)):
            result = M.inspect_candidate(
                self.h.worktree_item(),
                repo=str(self.h.repo),
                managed_root=str(self.h.managed),
                remote=remote,
                census=census,
                process_ok=True,
                process_reasons=[],
                by_pid=by_pid,
                locks=[],
                lock_error=None,
            )
        self.assertEqual(result["processes"], [])
        self.assertFalse(any(b.startswith("process-census-incomplete") for b in result["blockers"]))
        self.assertFalse(any(b.startswith("processes:") for b in result["blockers"]))

    def test_ignored_command_and_du_timeout_in_source(self) -> None:
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('--ignored=matching', src)
        self.assertIn('-unormal', src)
        self.assertIn("timeout=60", src)
        # Exact constant used for ignored inventory.
        self.assertEqual(
            M.IGNORED_STATUS_ARGV,
            ["git", "status", "--porcelain=v1", "-z", "--ignored=matching", "-unormal"],
        )
        # size_bytes uses 60s bound.
        with mock.patch.object(M, "run", side_effect=M.ProbeError("timeout:du:60s")):
            size, err = M.size_bytes("/tmp")
        self.assertIsNone(size)
        self.assertEqual(err, "timeout:du:60s")

    def test_du_timeout_constant_via_run(self) -> None:
        seen: list[float] = []

        def capture_run(argv: list[str], **kwargs: Any) -> Any:
            seen.append(float(kwargs.get("timeout", -1)))
            raise M.ProbeError("timeout:du:60s")

        with mock.patch.object(M, "run", side_effect=capture_run):
            M.size_bytes("/tmp")
        self.assertEqual(seen, [60.0])

    def test_main_emits_json_report(self) -> None:
        self.h.write_census()
        buf = io.StringIO()
        with mock.patch.object(M, "run", side_effect=self.h.fake_run), mock.patch.object(
            M, "checked", side_effect=self.h.fake_checked
        ), mock.patch.object(M, "size_bytes", return_value=(4096, None)), mock.patch(
            "sys.stdout", buf
        ):
            code = M.main(
                [
                    "--repo",
                    str(self.h.repo),
                    "--managed-root",
                    str(self.h.managed),
                    "--policy",
                    str(self.h.policy),
                    "--process-census",
                    str(self.h.census),
                    "--proc-root",
                    str(self.h.proc),
                    "--wait-seconds",
                    "0",
                    "--poll-seconds",
                    "0",
                ]
            )
            payload = json.loads(buf.getvalue())
        self.assertIn(code, (0, 2))
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("process_census", payload)
        self.assertIn("captured_at", payload["process_census"])
        self.assertIn("boot_id", payload["process_census"])
        self.assertIn("reasons", payload["process_census"])
        self.assertIn("started_at", payload)
        self.assertIn("worktrees", payload)
        # One well-formed JSON document (parse already succeeded).
        self.assertIsInstance(payload["complete"], bool)


class WaitBoundTests(unittest.TestCase):
    def test_default_wait_is_75s(self) -> None:
        self.assertEqual(M.SNAPSHOT_WAIT_S, 75)
        self.assertEqual(M.DEFAULT_CENSUS, "/run/paseo/process-census.json")


class TransientProcessProofRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.h.write_census()

    def tearDown(self) -> None:
        self.h.close()

    def _collect(self, wait_s: float = 2.0, poll_s: float = 0.05) -> tuple[bool, list[str], Any, dict[str, Any]]:
        return M.collect_process_census(
            census_path=str(self.h.census),
            proc_root=str(self.h.proc),
            managed_root=str(self.h.managed),
            common_git_dir=str(self.h.repo / ".git"),
            started_at=self.h.started,
            now_fn=lambda: self.h.now,
            wait_s=wait_s,
            poll_s=poll_s,
        )

    def test_transient_process_new_retries_with_newer_snapshot(self) -> None:
        pdir = self.h.proc / "2"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(2, "new", 50))
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
                        "uid": 0,
                        "scope_complete": True,
                        "references": [],
                    },
                    {
                        "pid": 2,
                        "start_time_ticks": 50,
                        "name": "new",
                        "uid": 1000,
                        "scope_complete": True,
                        "references": [],
                    },
                ]
                self.h.write_census(captured_at=iso(self.h.now + timedelta(seconds=1)))

        with mock.patch.object(M._asc.time, "sleep", side_effect=on_sleep):
            ok, reasons, _, summary = self._collect()
        self.assertTrue(ok, reasons)
        self.assertEqual(summary["status"], "ok")
        self.assertGreaterEqual(sleep_n["n"], 1)

    def test_persistent_transient_blocks_with_exact_final_reasons(self) -> None:
        pdir = self.h.proc / "2"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(2, "new", 50))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")
        with mock.patch.object(M._asc.time, "sleep", return_value=None):
            ok, reasons, _, summary = self._collect(wait_s=0.15, poll_s=0.01)
        self.assertFalse(ok)
        self.assertIn("process_new", reasons)
        self.assertEqual(summary["status"], "blocked")

    def test_mixed_transient_nontransient_blocks_immediately(self) -> None:
        pdir = self.h.proc / "2"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(2, "new", 50))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")
        self.h.write_census(roots=["/var/unrelated"])
        # Re-add live process after write_census reseeded proc.
        pdir = self.h.proc / "2"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(2, "new", 50))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")

        sleep_calls: list[float] = []
        with mock.patch.object(M._asc.time, "sleep", side_effect=lambda s: sleep_calls.append(s)):
            ok, reasons, _, _ = self._collect(wait_s=2.0)
        self.assertFalse(ok)
        self.assertIn("snapshot_roots_unrelated", reasons)
        self.assertIn("process_new", reasons)
        self.assertEqual(sleep_calls, [])

    def test_process_new_still_blocks_when_wait_zero(self) -> None:
        pdir = self.h.proc / "2"
        pdir.mkdir()
        wfile(pdir / "stat", make_stat(2, "new", 50))
        wfile(pdir / "cmdline", b"new\0")
        os.symlink("/bin/true", pdir / "exe")
        ok, reasons, _, _ = self._collect(wait_s=0.0, poll_s=0.0)
        self.assertFalse(ok)
        self.assertIn("process_new", reasons)


if __name__ == "__main__":
    unittest.main()
