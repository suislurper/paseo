#!/usr/bin/env python3
"""Deterministic fake-proc tests for process-census.py. No root required."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parent / "process-census.py"
SERVICE_UNIT = SCRIPT.parent / "systemd" / "paseo-process-census.service"


def load_census():
    spec = importlib.util.spec_from_file_location("process_census", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = load_census()


# ---------------------------------------------------------------------------
# Fake /proc builders
# ---------------------------------------------------------------------------


def write_file(path: Path, content: str | bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    os.chmod(path, mode)


def make_stat(pid: int, comm: str, start_time_ticks: int, state: str = "S") -> str:
    """Build a minimal /proc/pid/stat line with correct field positions."""
    # Fields after comm: state ppid pgrp session tty_nr tpgid flags
    # minflt cminflt majflt cmajflt utime stime cutime cstime priority nice
    # num_threads itrealvalue starttime ...
    # rest indices 0..19 where 19 is starttime
    rest = ["0"] * 20
    rest[0] = state
    rest[1] = "1"  # ppid
    rest[19] = str(start_time_ticks)
    return f"{pid} ({comm}) " + " ".join(rest) + "\n"


def make_status(uid: int = 1000) -> str:
    return f"Name:\ttest\nUmask:\t0022\nState:\tS (sleeping)\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"


def add_process(
    proc: Path,
    pid: int,
    *,
    comm: str = "app",
    start_time_ticks: int = 1000,
    uid: int = 1000,
    cmdline: list[str] | None = None,
    cwd: str | None = None,
    exe: str | None = None,
    fds: dict[int, str] | None = None,
    kernel_thread: bool = False,
    no_cmdline: bool = False,
    no_exe: bool = False,
    chmod_cmdline: int | None = None,
    chmod_fd_dir: int | None = None,
    chmod_stat: int | None = None,
) -> Path:
    pdir = proc / str(pid)
    pdir.mkdir(parents=True, exist_ok=True)
    write_file(pdir / "stat", make_stat(pid, comm, start_time_ticks))
    write_file(pdir / "status", make_status(uid))

    if kernel_thread:
        write_file(pdir / "cmdline", b"")
        # no exe symlink → kernel thread
    else:
        if not no_cmdline:
            if cmdline is None:
                cmdline = [f"/usr/bin/{comm}"]
            raw = b"\0".join(t.encode("utf-8") for t in cmdline) + b"\0"
            write_file(pdir / "cmdline", raw)
            if chmod_cmdline is not None:
                os.chmod(pdir / "cmdline", chmod_cmdline)
        if not no_exe:
            target = exe if exe is not None else f"/usr/bin/{comm}"
            if (pdir / "exe").exists() or (pdir / "exe").is_symlink():
                (pdir / "exe").unlink()
            os.symlink(target, pdir / "exe")
        if cwd is not None:
            if (pdir / "cwd").exists() or (pdir / "cwd").is_symlink():
                (pdir / "cwd").unlink()
            os.symlink(cwd, pdir / "cwd")
        fd_dir = pdir / "fd"
        fd_dir.mkdir(exist_ok=True)
        if fds:
            for num, target in fds.items():
                link = fd_dir / str(num)
                if link.exists() or link.is_symlink():
                    link.unlink()
                os.symlink(target, link)
        if chmod_fd_dir is not None:
            os.chmod(fd_dir, chmod_fd_dir)
    if chmod_stat is not None:
        os.chmod(pdir / "stat", chmod_stat)
    return pdir


def seed_boot_id(proc: Path, boot_id: str = "boot-test-001") -> None:
    write_file(proc / "sys" / "kernel" / "random" / "boot_id", boot_id + "\n")


class ProcessCensusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="census-test-")
        self.root = Path(self._tmpdir.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        seed_boot_id(self.proc)
        self.roots = [
            str(self.root / "worktrees"),
            str(self.root / "runtime"),
        ]
        for r in self.roots:
            Path(r).mkdir(parents=True, exist_ok=True)
        self.out = self.root / "run" / "paseo" / "process-census.json"

    def tearDown(self) -> None:
        # Restore perms so cleanup can remove restricted dirs
        for dirpath, dirnames, filenames in os.walk(self.root):
            try:
                os.chmod(dirpath, 0o755)
            except OSError:
                pass
            for name in filenames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o644)
                except OSError:
                    pass
        self._tmpdir.cleanup()

    def snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return census.build_snapshot(
            proc_root=str(self.proc),
            roots=kwargs.get("roots", self.roots),
            captured_at=kwargs.get("captured_at", "2026-08-06T00:00:00Z"),
        )

    def write_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        snap = self.snapshot(**kwargs)
        census.atomic_write_json(str(self.out), snap, mode=0o644)
        return snap

    # --- redaction ---

    def test_redaction_unrelated_process_empty_references(self) -> None:
        add_process(
            self.proc,
            100,
            comm="secret-app",
            cwd="/home/other/.ssh",
            exe="/usr/bin/secret-app",
            cmdline=["/usr/bin/secret-app", "--token", "supersecret", "/tmp/not-root"],
            fds={3: "/etc/shadow", 4: "socket:[12345]"},
        )
        snap = self.snapshot()
        self.assertTrue(snap["complete"])
        self.assertEqual(len(snap["processes"]), 1)
        proc = snap["processes"][0]
        self.assertEqual(proc["pid"], 100)
        self.assertEqual(proc["references"], [])
        blob = json.dumps(snap)
        self.assertNotIn("supersecret", blob)
        self.assertNotIn("/etc/shadow", blob)
        self.assertNotIn("/home/other/.ssh", blob)
        self.assertNotIn("--token", blob)
        self.assertNotIn("argv", blob)

    def test_relevant_references_under_roots(self) -> None:
        wt = self.roots[0]
        rt = self.roots[1]
        add_process(
            self.proc,
            200,
            comm="node",
            cwd=f"{wt}/proj",
            exe=f"{rt}/bin/node",
            cmdline=[f"{rt}/bin/node", f"{wt}/proj/server.js"],
            fds={
                1: "/dev/null",
                3: f"{wt}/proj/package.json",
                4: f"{rt}/logs/out.log",
                5: "/var/log/syslog",
            },
        )
        snap = self.snapshot()
        proc = snap["processes"][0]
        kinds_paths = {(r["kind"], r["path"]) for r in proc["references"]}
        self.assertIn(("cwd", f"{wt}/proj"), kinds_paths)
        self.assertIn(("exe", f"{rt}/bin/node"), kinds_paths)
        self.assertIn(("open_fd", f"{wt}/proj/package.json"), kinds_paths)
        self.assertIn(("open_fd", f"{rt}/logs/out.log"), kinds_paths)
        # Outside root suppressed
        self.assertNotIn(("open_fd", "/var/log/syslog"), kinds_paths)
        self.assertNotIn("/dev/null", json.dumps(snap))

    # --- interpreter scripts ---

    def test_interpreter_script_under_root(self) -> None:
        wt = self.roots[0]
        add_process(
            self.proc,
            300,
            comm="python3",
            cwd=f"{wt}/job",
            exe="/usr/bin/python3.12",
            cmdline=["/usr/bin/python3.12", "-u", f"{wt}/job/run.py", "--flag", "x"],
        )
        snap = self.snapshot()
        refs = snap["processes"][0]["references"]
        scripts = [r for r in refs if r["kind"] == "interpreter_script"]
        self.assertEqual(scripts, [{"kind": "interpreter_script", "path": f"{wt}/job/run.py"}])
        # Arbitrary args not emitted
        self.assertNotIn("--flag", json.dumps(snap))
        self.assertNotIn('"-u"', json.dumps(snap))

    def test_interpreter_script_outside_root_not_emitted(self) -> None:
        add_process(
            self.proc,
            301,
            comm="python3",
            cwd="/tmp",
            exe="/usr/bin/python3",
            cmdline=["/usr/bin/python3", "/tmp/evil.py"],
        )
        snap = self.snapshot()
        refs = snap["processes"][0]["references"]
        self.assertEqual([r for r in refs if r["kind"] == "interpreter_script"], [])
        self.assertNotIn("/tmp/evil.py", json.dumps(snap))

    def test_relative_interpreter_script_resolved_via_cwd(self) -> None:
        wt = self.roots[0]
        add_process(
            self.proc,
            302,
            comm="bash",
            cwd=f"{wt}/scripts",
            exe="/bin/bash",
            cmdline=["/bin/bash", "helper.sh"],
        )
        snap = self.snapshot()
        scripts = [
            r for r in snap["processes"][0]["references"] if r["kind"] == "interpreter_script"
        ]
        self.assertEqual(
            scripts,
            [{"kind": "interpreter_script", "path": f"{wt}/scripts/helper.sh"}],
        )

    # --- kernel threads ---

    def test_kernel_threads_skipped(self) -> None:
        add_process(self.proc, 2, comm="kthreadd", start_time_ticks=1, kernel_thread=True)
        add_process(
            self.proc,
            400,
            comm="userapp",
            start_time_ticks=50,
            cwd=self.roots[0],
        )
        snap = self.snapshot()
        pids = [p["pid"] for p in snap["processes"]]
        self.assertEqual(pids, [400])
        self.assertNotIn(2, pids)

    # --- exit races ---

    def test_exit_race_does_not_mark_incomplete(self) -> None:
        """Process disappears between listing and stat → skip, complete stays true."""
        add_process(self.proc, 500, comm="ephemeral", cwd=self.roots[0])
        # Simulate exit: remove the process dir after we know pid list would include it
        pdir = self.proc / "500"
        # Monkey-patch list_pids to return 500 then delete before scan
        real_list = census.list_pids

        def list_then_gone(proc_root: str) -> list[int]:
            pids = real_list(proc_root)
            # delete pid 500 if present
            if pdir.exists():
                for child in sorted(pdir.rglob("*"), reverse=True):
                    if child.is_symlink() or child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        child.rmdir()
                pdir.rmdir()
            return pids

        orig = census.list_pids
        try:
            census.list_pids = list_then_gone  # type: ignore[assignment]
            snap = self.snapshot()
        finally:
            census.list_pids = orig  # type: ignore[assignment]
        self.assertTrue(snap["complete"])
        self.assertEqual(snap["errors"], [])
        self.assertEqual(snap["processes"], [])

    # --- permission / incomplete ---

    def test_permission_error_marks_incomplete(self) -> None:
        # Unreadable fd directory while process still exists
        add_process(
            self.proc,
            600,
            comm="private",
            start_time_ticks=999,
            cwd=self.roots[0],
            chmod_fd_dir=0o000,
        )
        snap = self.snapshot()
        self.assertFalse(snap["complete"])
        self.assertEqual(len(snap["processes"]), 1)
        proc = snap["processes"][0]
        self.assertEqual(proc["pid"], 600)
        self.assertEqual(proc["start_time_ticks"], 999)
        self.assertEqual(proc.get("error"), "permission")
        # No sensitive fields on error records
        self.assertNotIn("references", proc)
        self.assertNotIn("name", proc)
        self.assertNotIn("uid", proc)
        self.assertTrue(any(e.get("class") == "permission" for e in snap["errors"]))

    # --- PID start identity ---

    def test_pid_start_time_ticks_identity(self) -> None:
        add_process(self.proc, 700, comm="a", start_time_ticks=111)
        add_process(self.proc, 701, comm="b", start_time_ticks=222)
        snap = self.snapshot()
        by_pid = {p["pid"]: p["start_time_ticks"] for p in snap["processes"]}
        self.assertEqual(by_pid[700], 111)
        self.assertEqual(by_pid[701], 222)
        self.assertEqual(snap["schema_version"], 1)
        self.assertEqual(snap["boot_id"], "boot-test-001")
        self.assertEqual(snap["roots"], self.roots)

    def test_comm_with_spaces_and_parens_parses_starttime(self) -> None:
        add_process(self.proc, 702, comm="my app (x)", start_time_ticks=4242)
        snap = self.snapshot()
        self.assertEqual(snap["processes"][0]["start_time_ticks"], 4242)
        self.assertEqual(snap["processes"][0]["name"], "my app (x)")

    # --- symlink / output refusal ---

    def test_refuse_symlink_output_path(self) -> None:
        target = self.root / "real-out.json"
        target.write_text("{}")
        link = self.root / "link-out.json"
        os.symlink(target, link)
        with self.assertRaises(census.CensusError):
            census.ensure_no_symlink_components(str(link))
        with self.assertRaises(census.CensusError):
            census.atomic_write_json(str(link), {"x": 1})

    def test_refuse_symlink_parent_component(self) -> None:
        real_dir = self.root / "real-run"
        real_dir.mkdir()
        link_parent = self.root / "link-run"
        os.symlink(real_dir, link_parent)
        out = link_parent / "process-census.json"
        with self.assertRaises(census.CensusError):
            census.ensure_no_symlink_components(str(out))

    def test_refuse_non_absolute_roots_and_output(self) -> None:
        with self.assertRaises(census.CensusError):
            census.require_absolute("relative/path", "root")
        rc = census.main(
            [
                "--root",
                "not-absolute",
                "--output",
                str(self.out),
                "--proc-root",
                str(self.proc),
            ]
        )
        self.assertEqual(rc, 2)

    # --- atomic file mode ---

    def test_atomic_write_mode_0644_and_parent_0755(self) -> None:
        snap = self.write_snapshot()
        self.assertTrue(self.out.is_file())
        mode = stat.S_IMODE(self.out.stat().st_mode)
        self.assertEqual(mode, 0o644)
        parent_mode = stat.S_IMODE(self.out.parent.stat().st_mode)
        self.assertEqual(parent_mode, 0o755)
        loaded = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(loaded["boot_id"], snap["boot_id"])
        # No leftover temp files
        leftovers = list(self.out.parent.glob(".process-census.*"))
        self.assertEqual(leftovers, [])

    def test_atomic_replace_overwrites(self) -> None:
        census.atomic_write_json(str(self.out), {"v": 1}, mode=0o644)
        census.atomic_write_json(str(self.out), {"v": 2}, mode=0o644)
        self.assertEqual(json.loads(self.out.read_text())["v"], 2)

    # --- top-level schema ---

    def test_snapshot_top_level_keys(self) -> None:
        add_process(self.proc, 1, comm="init", uid=0, start_time_ticks=0)
        snap = self.snapshot()
        for key in (
            "schema_version",
            "boot_id",
            "captured_at",
            "roots",
            "complete",
            "errors",
            "processes",
        ):
            self.assertIn(key, snap)

    def test_systemd_unit_preserves_snapshot_between_oneshots(self) -> None:
        unit = SERVICE_UNIT.read_text(encoding="utf-8")
        self.assertIn("RuntimeDirectory=paseo", unit)
        self.assertIn("RuntimeDirectoryPreserve=yes", unit)


if __name__ == "__main__":
    # Ensure we can import from the same directory when run as a script
    sys.path.insert(0, str(SCRIPT.parent))
    unittest.main(verbosity=2)
