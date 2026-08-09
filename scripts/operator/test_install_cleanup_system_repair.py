#!/usr/bin/env python3
"""Focused tests for install-cleanup-system-repair.sh.

Temp dirs and fakes only. No real sudo, systemd, live installer, runtime targets,
schedule, services, databases, or remote/ref mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parent / "install-cleanup-system-repair.sh"
CHILD_INSTALLER = Path(__file__).resolve().parent / "install-process-census-reboot-trigger.sh"
REPO_ROOT = Path(__file__).resolve().parents[2]

# Production path constants that must appear in the installer surface.
PROD_BACKUP_PARENT = "/mnt/data/paseo-runtime/artifacts/operator-install/backups"
PROD_DESTS = (
    "/home/user/.paseo/bin/worktree_cleanup_probe.py",
    "/home/user/.paseo/bin/agent-scratch-cleanup.py",
    "/home/user/.paseo/bin/legacy-tmp-quarantine.py",
    "/home/user/.paseo/WORKTREE_CLEANUP_POLICY.md",
    "/home/user/.paseo/AGENT_SCRATCH_CLEANUP_POLICY.md",
    "/home/user/.paseo/worktree-cleanup-wake.txt",
)

PIN_MAP = {
    "EXPECTED_CHILD_INSTALLER_SHA": CHILD_INSTALLER,
    "EXPECTED_TIMER_SHA": REPO_ROOT
    / "scripts/operator/systemd/paseo-process-census.timer",
    "EXPECTED_SERVICE_SHA": REPO_ROOT
    / "scripts/operator/systemd/paseo-process-census.service",
    "EXPECTED_HELPER_SHA": REPO_ROOT / "scripts/operator/process-census.py",
    "EXPECTED_DOC_SHA": REPO_ROOT / "docs/operator-fork.md",
    "EXPECTED_WORKTREE_PROBE_SHA": REPO_ROOT
    / "scripts/operator/worktree-cleanup-probe.py",
    "EXPECTED_AGENT_SCRATCH_SHA": REPO_ROOT
    / "scripts/operator/agent-scratch-cleanup.py",
    "EXPECTED_LEGACY_TMP_SHA": REPO_ROOT
    / "scripts/operator/legacy-tmp-quarantine.py",
    "EXPECTED_WORKTREE_POLICY_SHA": REPO_ROOT
    / "scripts/operator/policy/WORKTREE_CLEANUP_POLICY.md",
    "EXPECTED_SCRATCH_POLICY_SHA": REPO_ROOT
    / "scripts/operator/policy/AGENT_SCRATCH_CLEANUP_POLICY.md",
    "EXPECTED_WAKE_TXT_SHA": REPO_ROOT
    / "scripts/operator/policy/worktree-cleanup-wake.txt",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _bash(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=full_env,
    )


def _source_helpers_prefix() -> str:
    return textwrap.dedent(
        f"""\
        set -euo pipefail
        # shellcheck source=/dev/null
        source {INSTALLER.as_posix()!r}
        """
    )


def _parse_readonly_sha(name: str) -> str:
    text = INSTALLER.read_text(encoding="utf-8")
    m = re.search(rf"^readonly {re.escape(name)}=([0-9a-f]{{64}})\s*$", text, re.M)
    if not m:
        raise AssertionError(f"missing pin {name}")
    return m.group(1)


class InstallerSyntaxAndSurfaceTests(unittest.TestCase):
    def test_bash_n_clean(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(INSTALLER)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_installer_is_executable(self) -> None:
        mode = INSTALLER.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_main_not_run_when_sourced(self) -> None:
        script = _source_helpers_prefix() + "printf 'sourced-ok\\n'\n"
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "sourced-ok")

    def test_surface_child_only_timer_no_paseo_shab(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("install-process-census-reboot-trigger.sh", text)
        self.assertIn("paseo-process-census.timer", text)
        self.assertIn(PROD_BACKUP_PARENT, text)
        for dest in PROD_DESTS:
            self.assertIn(dest, text)
        # Must not restart Paseo/SHAB or mutate schedule.
        self.assertNotIn("systemctl restart paseo", text)
        self.assertNotIn("systemctl restart shab", text)
        self.assertNotIn("paseo schedule", text)
        self.assertIn("Paseo/SHAB services were not restarted", text)
        self.assertIn("schedule state was not mutated", text)
        # Child is invoked as a path in main (assignment + one execution capture).
        self.assertIn('child_out=$("$child_installer" 2>&1)', text)
        self.assertEqual(text.count('child_out=$("$child_installer" 2>&1)'), 1)
        # No broad deletion.
        self.assertNotIn("rm -rf", text)
        self.assertNotIn("rm -fr", text)

    def test_child_surface_timer_only(self) -> None:
        """Surface proves only child timer installer/systemd behavior."""
        child = CHILD_INSTALLER.read_text(encoding="utf-8")
        self.assertIn('systemctl stop -- "$TIMER_UNIT"', child)
        self.assertIn('systemctl start -- "$TIMER_UNIT"', child)
        self.assertIn('systemctl enable -- "$TIMER_UNIT"', child)
        self.assertNotIn('systemctl restart -- "$SERVICE_UNIT"', child)
        self.assertNotIn('systemctl stop -- "$SERVICE_UNIT"', child)
        self.assertNotIn("systemctl kill", child)
        self.assertNotIn("paseo schedule", child)
        # Explicit: child never restarts Paseo or non-census units.
        self.assertTrue(
            ("never Paseo" in child)
            or ("Never touch Paseo" in child)
            or ("never touch Paseo" in child.lower())
        )
        # No SHAB service control — /mnt/data/shab/.git is a census root only.
        self.assertNotIn("systemctl", "".join(ln for ln in child.splitlines() if "shab" in ln.lower()))


class PinRecomputeTests(unittest.TestCase):
    def test_pins_match_repo_sources(self) -> None:
        for name, path in PIN_MAP.items():
            self.assertTrue(path.is_file(), f"missing {path}")
            want = _sha256(path)
            got = _parse_readonly_sha(name)
            self.assertEqual(got, want, f"{name} pin mismatch for {path}")

    def test_child_doc_pin_matches_operator_fork(self) -> None:
        child = CHILD_INSTALLER.read_text(encoding="utf-8")
        m = re.search(r"^readonly EXPECTED_DOC_SHA=([0-9a-f]{64})\s*$", child, re.M)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m.group(1), _sha256(REPO_ROOT / "docs/operator-fork.md"))


class HelperShaAndPathTests(unittest.TestCase):
    def test_assert_sha_accepts_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "f"
            p.write_text("hello\n", encoding="utf-8")
            digest = _sha256(p)
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                assert_sha {p.as_posix()!r} {digest!r}
                printf 'ok\\n'
                """
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "ok")

    def test_assert_sha_rejects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "f"
            p.write_text("hello\n", encoding="utf-8")
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                assert_sha {p.as_posix()!r} {'0' * 64}
                """
            )
            proc = _bash(script)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("sha256 mismatch", proc.stderr)

    def test_path_rejects_symlink_component(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real = base / "real"
            real.mkdir()
            link = base / "link"
            link.symlink_to(real)
            nested = link / "child"
            nested.mkdir()
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                if path_has_no_symlink_components {nested.as_posix()!r}; then
                  printf 'yes\\n'
                else
                  printf 'no\\n'
                fi
                """
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "no")

    def test_path_accepts_real_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                if path_has_no_symlink_components {Path(td).as_posix()!r}; then
                  printf 'yes\\n'
                else
                  printf 'no\\n'
                fi
                """
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "yes")

    def test_assert_dest_not_symlink_rejects_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real = base / "realfile"
            real.write_text("x\n", encoding="utf-8")
            link = base / "linkfile"
            link.symlink_to(real)
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                assert_dest_not_symlink {link.as_posix()!r}
                """
            )
            proc = _bash(script)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("symlink", proc.stderr.lower())


class AtomicInstallTests(unittest.TestCase):
    def test_atomic_install_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            src = base / "src.py"
            dest = base / "dest.py"
            body = b"#!/usr/bin/env python3\nprint('probe')\n"
            src.write_bytes(body)
            digest = hashlib.sha256(body).hexdigest()
            user = os.environ.get("USER") or "user"
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                atomic_install_user_file {src.as_posix()!r} {dest.as_posix()!r} 755 {digest!r} {user!r}
                printf 'ok\\n'
                """
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stdout.strip(), "ok")
            self.assertTrue(dest.is_file())
            self.assertFalse(dest.is_symlink())
            self.assertEqual(dest.read_bytes(), body)
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o755)
            # No leftover temps
            leftovers = list(base.glob(".dest.py.tmp.*"))
            self.assertEqual(leftovers, [])

    def test_atomic_install_failure_preserves_old_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            src = base / "src.py"
            dest = base / "dest.py"
            old = b"old-content\n"
            dest.write_bytes(old)
            dest.chmod(0o644)
            src.write_bytes(b"new-content\n")
            # Wrong expected sha forces fail after temp write / during assert_sha on src.
            bad = "a" * 64
            user = os.environ.get("USER") or "user"
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                atomic_install_user_file {src.as_posix()!r} {dest.as_posix()!r} 755 {bad!r} {user!r}
                """
            )
            proc = _bash(script)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(dest.read_bytes(), old)
            leftovers = list(base.glob(".dest.py.tmp.*"))
            self.assertEqual(leftovers, [])

    def test_atomic_install_rejects_symlink_destination(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            src = base / "src.py"
            real = base / "real.py"
            dest = base / "dest.py"
            body = b"payload\n"
            src.write_bytes(body)
            real.write_bytes(b"other\n")
            dest.symlink_to(real)
            digest = hashlib.sha256(body).hexdigest()
            user = os.environ.get("USER") or "user"
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                atomic_install_user_file {src.as_posix()!r} {dest.as_posix()!r} 644 {digest!r} {user!r}
                """
            )
            proc = _bash(script)
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue(dest.is_symlink())
            self.assertEqual(real.read_bytes(), b"other\n")


class BackupInventoryTests(unittest.TestCase):
    def test_backup_preserves_preinstall_and_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            backup_parent = base / "backups"
            dest_dir = base / "dests"
            dest_dir.mkdir()
            dest = dest_dir / "worktree_cleanup_probe.py"
            pre = b"pre-install-bytes\n"
            dest.write_bytes(pre)
            dest.chmod(0o755)
            pre_sha = hashlib.sha256(pre).hexdigest()
            head = "a" * 40
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                create_user_payload_backup {head!r} {backup_parent.as_posix()!r} {dest.as_posix()!r}
                """
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            backup_dir = Path(proc.stdout.strip())
            self.assertTrue(backup_dir.is_dir())
            self.assertTrue(backup_dir.name.startswith(f"cleanup-system-repair-{head}-"))
            before = backup_dir / "worktree_cleanup_probe.py.before"
            self.assertTrue(before.is_file())
            self.assertEqual(before.read_bytes(), pre)
            inv = json.loads((backup_dir / "inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(len(inv), 1)
            self.assertEqual(inv[0]["path"], str(dest))
            self.assertTrue(inv[0]["present"])
            self.assertEqual(inv[0]["sha256"], pre_sha)
            self.assertIsNone(inv[0]["installed_sha256"])

    def test_backup_absent_target_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            backup_parent = base / "backups"
            dest = base / "missing.py"
            head = "b" * 40
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                create_user_payload_backup {head!r} {backup_parent.as_posix()!r} {dest.as_posix()!r}
                """
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            backup_dir = Path(proc.stdout.strip())
            inv = json.loads((backup_dir / "inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(inv[0]["present"], False)
            self.assertFalse((backup_dir / "missing.py.before").exists())

    def test_backup_never_overwrites_existing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            backup_parent = base / "backups"
            dest = base / "f.py"
            dest.write_text("x\n", encoding="utf-8")
            head = "c" * 40
            # Freeze timestamp by creating the exact dir name the helper would use.
            # Call helper twice quickly may still differ by second; force collision
            # by pre-creating a dir with a matching prefix via stubbing date.
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                date() {{
                  if [[ "$*" == *'+%Y%m%dT%H%M%SZ'* ]]; then
                    printf '20260101T000000Z\\n'
                    return 0
                  fi
                  command date "$@"
                }}
                create_user_payload_backup {head!r} {backup_parent.as_posix()!r} {dest.as_posix()!r}
                # Second call with same frozen timestamp must fail (subshell: die exits).
                set +e
                err=$(create_user_payload_backup {head!r} {backup_parent.as_posix()!r} {dest.as_posix()!r} 2>&1)
                rc=$?
                set -e
                [[ "$rc" -ne 0 ]] || {{ printf 'unexpected-success\\n'; exit 2; }}
                printf '%s\\n' "$err" >&2
                printf 'conflict-ok\\n'
                """
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("conflict-ok", proc.stdout)
            self.assertIn("already exists", proc.stderr)
            # Exactly one backup dir
            dirs = list(backup_parent.glob(f"cleanup-system-repair-{head}-*"))
            self.assertEqual(len(dirs), 1)

    def test_finalize_inventory_sets_installed_shas(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            inv_path = base / "inventory.json"
            dest = "/tmp/example-dest"
            rows = [{"path": dest, "present": False, "installed_sha256": None}]
            inv_path.write_text(json.dumps(rows) + "\n", encoding="utf-8")
            sha = "d" * 64
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                finalize_inventory_installed_shas {inv_path.as_posix()!r} {dest!r} {sha!r}
                printf 'ok\\n'
                """
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(inv_path.read_text(encoding="utf-8"))
            self.assertEqual(data[0]["installed_sha256"], sha)


class SudoGateOrderTests(unittest.TestCase):
    def test_sudo_n_failure_before_backup_child_mutation(self) -> None:
        """sudo -n failure must occur before backup/child/target mutation."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            marker = base / "mutated"
            fake_bin = base / "bin"
            fake_bin.mkdir()
            # sudo that fails and must not be bypassed
            sudo_path = fake_bin / "sudo"
            sudo_path.write_text(
                "#!/usr/bin/env bash\necho 'sudo-denied' >&2\nexit 1\n",
                encoding="utf-8",
            )
            sudo_path.chmod(0o755)
            # Fake git so we never touch real remotes; also detect if called after sudo fail
            git_path = fake_bin / "git"
            git_path.write_text(
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    echo git-called >>{marker.as_posix()!r}
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            git_path.chmod(0o755)
            # Minimal source_root that passes directory checks if reached — but
            # sudo fails first after identity checks on real /home/user.
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                export PATH={fake_bin.as_posix()!r}:"$PATH"
                # Record whether create_user_payload_backup is reached.
                create_user_payload_backup() {{
                  echo backup-called >>{marker.as_posix()!r}
                  die "backup should not run"
                }}
                # Outer harness may catch only while asserting exact rc=1 and no marker.
                # Prove the actual preflight function returns nonzero (not just harness).
                set +e
                out=$(preflight_identity_and_git /tmp 2>&1)
                rc=$?
                set -e
                printf 'rc=%s\\n' "$rc"
                printf '%s\\n' "$out"
                if [[ -f {marker.as_posix()!r} ]]; then
                  printf 'marker=\\n'
                  cat {marker.as_posix()!r}
                else
                  printf 'marker=absent\\n'
                fi
                # Exact nonzero from preflight_identity_and_git (die → exit 1).
                [[ "$rc" -eq 1 ]] || {{ printf 'preflight-rc-not-1\\n'; exit 2; }}
                printf 'harness-ok\\n'
                """
            )
            proc = _bash(script)
            combined = proc.stderr + proc.stdout
            self.assertEqual(proc.returncode, 0, combined)
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("harness-ok", proc.stdout)
            self.assertNotIn("preflight-rc-not-1", combined)
            self.assertIn("sudo -n not available", combined)
            self.assertNotIn("backup-called", combined)
            self.assertNotIn("git-called", combined)
            self.assertIn("marker=absent", proc.stdout)
            if marker.exists():
                content = marker.read_text(encoding="utf-8")
                self.assertNotIn("backup-called", content)
                self.assertNotIn("git-called", content)


class PreflightGitGateTests(unittest.TestCase):
    REQUIRED_BRANCH = "fix-process-census-reboot-trigger"

    def _make_repo(self, root: Path, *, remote_url: str, head_msg: str = "init") -> str:
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "t@example.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "t"],
            check=True,
            capture_output=True,
        )
        (root / "README").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "README"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", head_msg],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "fork", remote_url],
            check=True,
            capture_output=True,
        )
        head = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        return head

    def _fake_sudo_bin(self, base: Path) -> Path:
        fake_bin = base / "bin"
        fake_bin.mkdir(exist_ok=True)
        sudo_path = fake_bin / "sudo"
        sudo_path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        sudo_path.chmod(0o755)
        return fake_bin

    def _make_linked_worktree(
        self,
        base: Path,
        *,
        branch: str,
        remote_url: str = "https://github.com/suislurper/paseo.git",
        detach: bool = False,
    ) -> Path:
        """Create a main repo + linked worktree; return the worktree path."""
        main = base / "main"
        main.mkdir()
        self._make_repo(main, remote_url=remote_url)
        wt = base / "wt"
        if detach:
            subprocess.run(
                ["git", "-C", str(main), "worktree", "add", "--detach", str(wt)],
                check=True,
                capture_output=True,
            )
        else:
            # Create/checkout the named branch in the linked worktree.
            subprocess.run(
                ["git", "-C", str(main), "branch", "-M", "main-seed"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(main), "worktree", "add", "-b", branch, str(wt)],
                check=True,
                capture_output=True,
            )
        return wt

    def test_dirty_checkout_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # Dirty linked worktree on required branch still fails clean gate.
            wt = self._make_linked_worktree(base, branch=self.REQUIRED_BRANCH)
            (wt / "dirt").write_text("untracked\n", encoding="utf-8")
            marker = base / "marker"
            fake_bin = self._fake_sudo_bin(base)
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                export PATH={fake_bin.as_posix()!r}:"$PATH"
                create_user_payload_backup() {{
                  echo backup >>{marker.as_posix()!r}
                  die unexpected
                }}
                preflight_identity_and_git {wt.as_posix()!r}
                """
            )
            proc = _bash(script)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not clean", proc.stderr)
            self.assertFalse(marker.exists())

    def test_wrong_remote_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            wt = self._make_linked_worktree(
                base,
                branch=self.REQUIRED_BRANCH,
                remote_url="https://github.com/getpaseo/paseo.git",
            )
            marker = base / "marker"
            fake_bin = self._fake_sudo_bin(base)
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                export PATH={fake_bin.as_posix()!r}:"$PATH"
                create_user_payload_backup() {{
                  echo backup >>{marker.as_posix()!r}
                  die unexpected
                }}
                preflight_identity_and_git {wt.as_posix()!r}
                """
            )
            proc = _bash(script)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("remote fork must be", proc.stderr)
            self.assertFalse(marker.exists())

    def test_remote_head_mismatch_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            wt = self._make_linked_worktree(base, branch=self.REQUIRED_BRANCH)
            head = subprocess.check_output(
                ["git", "-C", str(wt), "rev-parse", "HEAD"], text=True
            ).strip()
            other = "f" * 40
            self.assertNotEqual(head, other)
            fake_bin = self._fake_sudo_bin(base)
            marker = base / "marker"
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                export PATH={fake_bin.as_posix()!r}:"$PATH"
                create_user_payload_backup() {{
                  echo backup >>{marker.as_posix()!r}
                  die unexpected
                }}
                git() {{
                  local root={wt.as_posix()!r}
                  if [[ "$1" == "-C" ]]; then
                    root=$2
                    shift 2
                  fi
                  if [[ "$1" == "ls-remote" ]]; then
                    printf '%s\\trefs/heads/main\\n' {other!r}
                    return 0
                  fi
                  command git -C "$root" "$@"
                }}
                preflight_identity_and_git {wt.as_posix()!r}
                """
            )
            proc = _bash(script)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("local HEAD", proc.stderr)
            self.assertIn("fork/main", proc.stderr)
            self.assertFalse(marker.exists())

    def test_main_shared_checkout_fails(self) -> None:
        """Main/shared checkout (git-dir == common-dir) fails closed."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "repo"
            repo.mkdir()
            self._make_repo(repo, remote_url="https://github.com/suislurper/paseo.git")
            # Rename to required branch so failure is specifically git-dir==common-dir.
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-M", self.REQUIRED_BRANCH],
                check=True,
                capture_output=True,
            )
            fake_bin = self._fake_sudo_bin(base)
            marker = base / "marker"
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                export PATH={fake_bin.as_posix()!r}:"$PATH"
                create_user_payload_backup() {{
                  echo backup >>{marker.as_posix()!r}
                  die unexpected
                }}
                set +e
                out=$(preflight_identity_and_git {repo.as_posix()!r} 2>&1)
                rc=$?
                set -e
                printf 'rc=%s\\n' "$rc"
                printf '%s\\n' "$out" >&2
                """
            )
            proc = _bash(script)
            combined = proc.stderr + proc.stdout
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("git-dir equals common-dir", combined)
            self.assertFalse(marker.exists())

    def test_detached_head_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            wt = self._make_linked_worktree(base, branch=self.REQUIRED_BRANCH, detach=True)
            fake_bin = self._fake_sudo_bin(base)
            marker = base / "marker"
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                export PATH={fake_bin.as_posix()!r}:"$PATH"
                create_user_payload_backup() {{
                  echo backup >>{marker.as_posix()!r}
                  die unexpected
                }}
                set +e
                out=$(preflight_identity_and_git {wt.as_posix()!r} 2>&1)
                rc=$?
                set -e
                printf 'rc=%s\\n' "$rc"
                printf '%s\\n' "$out" >&2
                """
            )
            proc = _bash(script)
            combined = proc.stderr + proc.stdout
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("detached", combined.lower())
            self.assertFalse(marker.exists())

    def test_wrong_branch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            wt = self._make_linked_worktree(base, branch="not-the-required-branch")
            fake_bin = self._fake_sudo_bin(base)
            marker = base / "marker"
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                export PATH={fake_bin.as_posix()!r}:"$PATH"
                create_user_payload_backup() {{
                  echo backup >>{marker.as_posix()!r}
                  die unexpected
                }}
                set +e
                out=$(preflight_identity_and_git {wt.as_posix()!r} 2>&1)
                rc=$?
                set -e
                printf 'rc=%s\\n' "$rc"
                printf '%s\\n' "$out" >&2
                """
            )
            proc = _bash(script)
            combined = proc.stderr + proc.stdout
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("branch must be fix-process-census-reboot-trigger", combined)
            self.assertFalse(marker.exists())

    def test_top_level_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            wt = self._make_linked_worktree(base, branch=self.REQUIRED_BRANCH)
            other = base / "other-root"
            other.mkdir()
            fake_bin = self._fake_sudo_bin(base)
            marker = base / "marker"
            # Intercept only --show-toplevel so resolved top-level != source_root.
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                export PATH={fake_bin.as_posix()!r}:"$PATH"
                create_user_payload_backup() {{
                  echo backup >>{marker.as_posix()!r}
                  die unexpected
                }}
                git() {{
                  local root=""
                  if [[ "$1" == "-C" ]]; then
                    root=$2
                    shift 2
                  fi
                  if [[ "$1" == "rev-parse" ]]; then
                    local a
                    for a in "$@"; do
                      if [[ "$a" == "--show-toplevel" ]]; then
                        printf '%s\\n' {other.as_posix()!r}
                        return 0
                      fi
                    done
                  fi
                  if [[ -n "$root" ]]; then
                    command git -C "$root" "$@"
                  else
                    command git "$@"
                  fi
                }}
                set +e
                out=$(preflight_identity_and_git {wt.as_posix()!r} 2>&1)
                rc=$?
                set -e
                printf 'rc=%s\\n' "$rc"
                printf '%s\\n' "$out" >&2
                """
            )
            proc = _bash(script)
            combined = proc.stderr + proc.stdout
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("does not equal source_root", combined)
            self.assertFalse(marker.exists())

    def test_payload_hash_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # Build a full source tree with wrong bytes so pin checks fail closed.
            op = base / "scripts" / "operator"
            systemd = op / "systemd"
            policy = op / "policy"
            docs = base / "docs"
            systemd.mkdir(parents=True)
            policy.mkdir(parents=True)
            docs.mkdir(parents=True)
            files = {
                op / "install-process-census-reboot-trigger.sh": b"wrong-child\n",
                systemd / "paseo-process-census.timer": b"wrong-timer\n",
                systemd / "paseo-process-census.service": b"wrong-service\n",
                op / "process-census.py": b"wrong-helper\n",
                docs / "operator-fork.md": b"wrong-doc\n",
                op / "worktree-cleanup-probe.py": b"wrong-probe\n",
                op / "agent-scratch-cleanup.py": b"wrong-scratch\n",
                op / "legacy-tmp-quarantine.py": b"wrong-legacy\n",
                policy / "WORKTREE_CLEANUP_POLICY.md": b"wrong-wp\n",
                policy / "AGENT_SCRATCH_CLEANUP_POLICY.md": b"wrong-sp\n",
                policy / "worktree-cleanup-wake.txt": b"wrong-wake\n",
            }
            for path, body in files.items():
                path.write_bytes(body)
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                set +e
                err=$(preflight_payload_pins {base.as_posix()!r} {op.as_posix()!r} 2>&1)
                rc=$?
                set -e
                printf 'rc=%s\\n' "$rc"
                printf '%s\\n' "$err" >&2
                """
            )
            proc = _bash(script)
            # outer shell may still exit 0 after set +e capture; check printed rc + message
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("sha256 mismatch", proc.stderr + proc.stdout)


class DestinationAllowlistTests(unittest.TestCase):
    def test_production_dest_allowlist_and_modes(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("DEST_WORKTREE_PROBE=/home/user/.paseo/bin/worktree_cleanup_probe.py", text)
        self.assertIn("DEST_AGENT_SCRATCH=/home/user/.paseo/bin/agent-scratch-cleanup.py", text)
        self.assertIn("DEST_LEGACY_TMP=/home/user/.paseo/bin/legacy-tmp-quarantine.py", text)
        self.assertIn("DEST_WORKTREE_POLICY=/home/user/.paseo/WORKTREE_CLEANUP_POLICY.md", text)
        self.assertIn("DEST_SCRATCH_POLICY=/home/user/.paseo/AGENT_SCRATCH_CLEANUP_POLICY.md", text)
        self.assertIn("DEST_WAKE_TXT=/home/user/.paseo/worktree-cleanup-wake.txt", text)
        # mode arguments in main installs
        self.assertIn('atomic_install_user_file "$worktree_probe_src" "$DEST_WORKTREE_PROBE" 755', text)
        self.assertIn('atomic_install_user_file "$agent_scratch_src" "$DEST_AGENT_SCRATCH" 755', text)
        self.assertIn('atomic_install_user_file "$legacy_tmp_src" "$DEST_LEGACY_TMP" 755', text)
        self.assertIn('atomic_install_user_file "$worktree_policy_src" "$DEST_WORKTREE_POLICY" 644', text)
        self.assertIn('atomic_install_user_file "$scratch_policy_src" "$DEST_SCRATCH_POLICY" 644', text)
        self.assertIn('atomic_install_user_file "$wake_src" "$DEST_WAKE_TXT" 644', text)
        self.assertIn("EXPECTED_USER=user", text)
        self.assertIn("EXPECTED_HOME=/home/user", text)

    def test_reject_wrong_user_home_constants(self) -> None:
        """Identity gate fails closed for wrong user/home (message contract)."""
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('install user must be ${EXPECTED_USER}', text)
        self.assertIn("HOME must be ${EXPECTED_HOME}", text)
        self.assertIn("must not run as root", text)

    def test_dest_preflight_precedes_child_in_main(self) -> None:
        """Surface/order: destination preflight textually precedes the child call."""
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("preflight_destinations_and_backup_parents", text)
        # Function definition exists.
        self.assertIn("preflight_destinations_and_backup_parents()", text)
        # Main body order: payload pins → dest preflight → child invocation.
        pin_idx = text.find("preflight_payload_pins \"$source_root\" \"$script_dir\"")
        dest_idx = text.find("preflight_destinations_and_backup_parents \"$BACKUP_PARENT\"")
        child_idx = text.find('child_out=$("$child_installer" 2>&1)')
        self.assertGreater(pin_idx, 0)
        self.assertGreater(dest_idx, pin_idx)
        self.assertGreater(child_idx, dest_idx)
        # Exactly one child invocation capture.
        self.assertEqual(text.count('child_out=$("$child_installer" 2>&1)'), 1)


class DestinationBackupPreflightTests(unittest.TestCase):
    """Unit tests for preflight_destinations_and_backup_parents."""

    def _run_preflight(self, backup_parent: Path, *dests: Path) -> subprocess.CompletedProcess[str]:
        dest_args = " ".join(f"{d.as_posix()!r}" for d in dests)
        script = _source_helpers_prefix() + textwrap.dedent(
            f"""\
            set +e
            out=$(preflight_destinations_and_backup_parents \\
              {backup_parent.as_posix()!r} {dest_args} 2>&1)
            rc=$?
            set -e
            printf 'rc=%s\\n' "$rc"
            printf '%s\\n' "$out"
            """
        )
        return _bash(script)

    def test_absent_dest_with_real_parent_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = base / "dests"
            parent.mkdir()
            dest = parent / "probe.py"
            backup = base / "backups"  # absent is fine
            proc = self._run_preflight(backup, dest)
            self.assertIn("rc=0", proc.stdout, proc.stderr + proc.stdout)

    def test_user_owned_regular_dest_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = base / "dests"
            parent.mkdir()
            dest = parent / "probe.py"
            dest.write_text("existing\n", encoding="utf-8")
            backup = base / "backups"
            backup.mkdir()
            # Owner is current user; EXPECTED_USER is user — require match.
            user = os.environ.get("USER") or "user"
            if user != "user":
                self.skipTest("EXPECTED_USER is hard-coded to user")
            proc = self._run_preflight(backup, dest)
            self.assertIn("rc=0", proc.stdout, proc.stderr + proc.stdout)

    def test_directory_dest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = base / "dests"
            parent.mkdir()
            dest = parent / "probe.py"
            dest.mkdir()  # directory, not regular file
            backup = base / "backups"
            proc = self._run_preflight(backup, dest)
            combined = proc.stderr + proc.stdout
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("regular file", combined)

    def test_symlink_dest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = base / "dests"
            parent.mkdir()
            real = parent / "real.py"
            real.write_text("x\n", encoding="utf-8")
            dest = parent / "probe.py"
            dest.symlink_to(real)
            backup = base / "backups"
            proc = self._run_preflight(backup, dest)
            combined = proc.stderr + proc.stdout
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("symlink", combined.lower())

    def test_special_file_dest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = base / "dests"
            parent.mkdir()
            dest = parent / "probe.py"
            os.mkfifo(dest)
            backup = base / "backups"
            proc = self._run_preflight(backup, dest)
            combined = proc.stderr + proc.stdout
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("regular file", combined)

    def test_wrong_owner_dest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = base / "dests"
            parent.mkdir()
            dest = parent / "probe.py"
            dest.write_text("x\n", encoding="utf-8")
            backup = base / "backups"
            # Mock stat so owner appears as root:root for the dest path.
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                stat() {{
                  if [[ "$*" == *'%U:%G'* && "$*" == *{dest.as_posix()!r}* ]]; then
                    printf 'root:root\\n'
                    return 0
                  fi
                  command stat "$@"
                }}
                set +e
                out=$(preflight_destinations_and_backup_parents \\
                  {backup.as_posix()!r} {dest.as_posix()!r} 2>&1)
                rc=$?
                set -e
                printf 'rc=%s\\n' "$rc"
                printf '%s\\n' "$out"
                """
            )
            proc = _bash(script)
            combined = proc.stderr + proc.stdout
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("owner must be", combined)

    def test_symlink_parent_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real = base / "real"
            real.mkdir()
            link = base / "link"
            link.symlink_to(real)
            dest = link / "probe.py"
            backup = base / "backups"
            proc = self._run_preflight(backup, dest)
            combined = proc.stderr + proc.stdout
            self.assertIn("rc=1", proc.stdout)
            self.assertTrue(
                "symlink" in combined.lower() or "not a directory" in combined,
                msg=combined,
            )

    def test_backup_parent_symlink_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = base / "dests"
            parent.mkdir()
            dest = parent / "probe.py"
            real_bp = base / "real-backups"
            real_bp.mkdir()
            backup = base / "backups"
            backup.symlink_to(real_bp)
            proc = self._run_preflight(backup, dest)
            combined = proc.stderr + proc.stdout
            self.assertIn("rc=1", proc.stdout)
            self.assertIn("symlink", combined.lower())

    def test_backup_parent_existing_user_dir_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = base / "dests"
            parent.mkdir()
            dest = parent / "probe.py"
            backup = base / "backups"
            backup.mkdir()
            user = os.environ.get("USER") or "user"
            if user != "user":
                self.skipTest("EXPECTED_USER is hard-coded to user")
            proc = self._run_preflight(backup, dest)
            self.assertIn("rc=0", proc.stdout, proc.stderr + proc.stdout)

    def test_no_mutation_on_preflight(self) -> None:
        """Preflight must not create backup parent or destination."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            parent = base / "dests"
            parent.mkdir()
            dest = parent / "probe.py"
            backup = base / "backups"
            self.assertFalse(dest.exists())
            self.assertFalse(backup.exists())
            proc = self._run_preflight(backup, dest)
            self.assertIn("rc=0", proc.stdout, proc.stderr + proc.stdout)
            self.assertFalse(dest.exists())
            self.assertFalse(backup.exists())


class EndToEndFakeRootTests(unittest.TestCase):
    def test_atomic_user_payload_flow_with_backup(self) -> None:
        """Full backup → install → inventory finalize with fakes/temp roots."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            dest_dir = base / "home" / "user" / ".paseo" / "bin"
            dest_dir.mkdir(parents=True)
            policy_dir = base / "home" / "user" / ".paseo"
            backup_parent = base / "backups"
            src_dir = base / "src"
            src_dir.mkdir()

            payloads = {
                "probe.py": (b"probe-v2\n", 0o755),
                "scratch.py": (b"scratch-v2\n", 0o755),
                "legacy.py": (b"legacy-v2\n", 0o755),
                "wp.md": (b"wp-v2\n", 0o644),
                "sp.md": (b"sp-v2\n", 0o644),
                "wake.txt": (b"wake-v2\n", 0o644),
            }
            dests = {
                "probe.py": dest_dir / "worktree_cleanup_probe.py",
                "scratch.py": dest_dir / "agent-scratch-cleanup.py",
                "legacy.py": dest_dir / "legacy-tmp-quarantine.py",
                "wp.md": policy_dir / "WORKTREE_CLEANUP_POLICY.md",
                "sp.md": policy_dir / "AGENT_SCRATCH_CLEANUP_POLICY.md",
                "wake.txt": policy_dir / "worktree-cleanup-wake.txt",
            }
            # Pre-install older files for first three
            for key in ("probe.py", "scratch.py", "legacy.py"):
                dests[key].write_bytes(b"old-" + payloads[key][0])
                dests[key].chmod(0o755)

            srcs = {}
            shas = {}
            for name, (body, _mode) in payloads.items():
                p = src_dir / name
                p.write_bytes(body)
                srcs[name] = p
                shas[name] = hashlib.sha256(body).hexdigest()

            user = os.environ.get("USER") or "user"
            head = "e" * 40
            dest_list = " ".join(f"{d.as_posix()!r}" for d in dests.values())
            install_cmds = []
            for name, mode in (
                ("probe.py", 755),
                ("scratch.py", 755),
                ("legacy.py", 755),
                ("wp.md", 644),
                ("sp.md", 644),
                ("wake.txt", 644),
            ):
                install_cmds.append(
                    f"atomic_install_user_file {srcs[name].as_posix()!r} "
                    f"{dests[name].as_posix()!r} {mode} {shas[name]!r} {user!r}"
                )
            finalize_pairs = " ".join(
                f"{dests[name].as_posix()!r} {shas[name]!r}" for name in payloads
            )
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                backup_dir=$(create_user_payload_backup {head!r} {backup_parent.as_posix()!r} {dest_list})
                {chr(10).join(install_cmds)}
                finalize_inventory_installed_shas "$backup_dir/inventory.json" {finalize_pairs}
                printf 'backup=%s\\n' "$backup_dir"
                printf 'PASS\\n'
                """
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("PASS", proc.stdout)
            # Destinations updated
            for name, (body, mode) in payloads.items():
                self.assertEqual(dests[name].read_bytes(), body)
                self.assertEqual(stat.S_IMODE(dests[name].stat().st_mode), mode)
            # Backup preserved old probe
            backup_line = [ln for ln in proc.stdout.splitlines() if ln.startswith("backup=")][0]
            backup_dir = Path(backup_line.split("=", 1)[1])
            old = backup_dir / "worktree_cleanup_probe.py.before"
            self.assertTrue(old.is_file())
            self.assertEqual(old.read_bytes(), b"old-probe-v2\n")
            inv = json.loads((backup_dir / "inventory.json").read_text(encoding="utf-8"))
            by_path = {row["path"]: row for row in inv}
            self.assertEqual(
                by_path[str(dests["probe.py"])]["installed_sha256"], shas["probe.py"]
            )
            self.assertTrue(by_path[str(dests["probe.py"])]["present"])
            # absent policies pre-install
            self.assertFalse(by_path[str(dests["wp.md"])]["present"])
            self.assertEqual(
                by_path[str(dests["wp.md"])]["installed_sha256"], shas["wp.md"]
            )


class InjectedFailureCleanupTests(unittest.TestCase):
    def test_failure_mid_install_leaves_no_temp_partial(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            src = base / "src"
            dest = base / "dest"
            src.write_bytes(b"good\n")
            dest.write_bytes(b"previous\n")
            good_sha = hashlib.sha256(b"good\n").hexdigest()
            user = os.environ.get("USER") or "user"
            # Inject failure by making fsync fail via a python wrapper on PATH?
            # Simpler: wrong owner expectation after copy — use expect_user that cannot match.
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                set +e
                atomic_install_user_file {src.as_posix()!r} {dest.as_posix()!r} 755 {good_sha!r} not-a-real-owner-xyz
                rc=$?
                set -e
                printf 'rc=%s\\n' "$rc"
                """
            )
            proc = _bash(script)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(dest.read_bytes(), b"previous\n")
            self.assertEqual(list(base.glob(".dest.tmp.*")), [])


class ChildDocPinTests(unittest.TestCase):
    def test_child_bash_n_still_clean(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(CHILD_INSTALLER)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
