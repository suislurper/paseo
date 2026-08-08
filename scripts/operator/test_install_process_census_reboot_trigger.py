#!/usr/bin/env python3
"""Focused tests for install-process-census-reboot-trigger.sh helpers.

No sudo. No real systemd. Temporary directories only.
Proves PCT-002 baseline exclusion and PCT-001 staging-path validation.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parent / "install-process-census-reboot-trigger.sh"


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


def write_census(
    path: Path,
    *,
    captured_at: str,
    boot_id: str,
    complete: bool = True,
    errors: list | None = None,
    processes: list | None = None,
    extra: dict | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "captured_at": captured_at,
        "boot_id": boot_id,
        "complete": complete,
        "errors": [] if errors is None else errors,
        "processes": [] if processes is None else processes,
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")


class StagingPathValidationTests(unittest.TestCase):
    def _valid(self, path: str) -> bool:
        script = _source_helpers_prefix() + textwrap.dedent(
            f"""\
            if is_valid_staging_dir {path!r}; then
              printf 'yes\\n'
            else
              printf 'no\\n'
            fi
            """
        )
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip() == "yes"

    def test_accepts_exact_parent_and_prefix(self) -> None:
        self.assertTrue(self._valid("/var/tmp/paseo-process-census-install.abc123"))

    def test_rejects_wrong_parent(self) -> None:
        self.assertFalse(self._valid("/tmp/paseo-process-census-install.abc123"))
        self.assertFalse(self._valid("/var/tmp/evil/paseo-process-census-install.abc123"))

    def test_rejects_wrong_prefix_and_relative(self) -> None:
        self.assertFalse(self._valid("/var/tmp/other-prefix.abc123"))
        self.assertFalse(self._valid("paseo-process-census-install.abc123"))
        self.assertFalse(self._valid("/var/tmp/../tmp/paseo-process-census-install.abc123"))


class CaptureIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pct-install-test-")
        self.root = Path(self.tmp.name)
        self.boot = "boot-test-fixed-001"
        self.census = self.root / "run" / "paseo" / "process-census.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _identity(self, path: Path) -> str:
        script = _source_helpers_prefix() + textwrap.dedent(
            f"""\
            census_file_identity {path.as_posix()!r}
            printf '\\n'
            """
        )
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.rstrip("\n")

    def _valid_identity(self, path: Path, boot_id: str) -> str:
        script = _source_helpers_prefix() + textwrap.dedent(
            f"""\
            valid_census_capture_identity {path.as_posix()!r} {boot_id!r}
            printf '\\n'
            """
        )
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.rstrip("\n")

    def _accept(self, baseline: str, first: str, candidate: str) -> str:
        # Pass identities via env to avoid shell-quoting edge cases with tabs.
        script = _source_helpers_prefix() + textwrap.dedent(
            """\
            accept_capture_step "$BASELINE" "$FIRST" "$CANDIDATE"
            """
        )
        proc = _bash(
            script,
            env={
                "BASELINE": baseline,
                "FIRST": first,
                "CANDIDATE": candidate,
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_missing_file_identity_empty(self) -> None:
        missing = self.root / "nope.json"
        self.assertEqual(self._identity(missing), "")
        self.assertEqual(self._valid_identity(missing, self.boot), "")

    def test_identity_includes_captured_at_stat_and_hash(self) -> None:
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:00Z",
            boot_id=self.boot,
        )
        ident = self._identity(self.census)
        parts = ident.split("\t")
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], "2026-08-01T00:00:00Z")
        # dev:ino:size:mtime
        self.assertRegex(parts[1], r"^\d+:\d+:\d+:\d+$")
        self.assertRegex(parts[2], r"^[0-9a-f]{64}$")

    def test_same_captured_at_replacement_changes_identity(self) -> None:
        """Identical timestamp must not fool identity if content/inode change."""
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:00Z",
            boot_id=self.boot,
            processes=[{"pid": 1}],
        )
        first = self._identity(self.census)
        # Unlink + rewrite forces new inode; same captured_at, different body.
        self.census.unlink()
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:00Z",
            boot_id=self.boot,
            processes=[{"pid": 2}],
        )
        second = self._identity(self.census)
        self.assertNotEqual(first, second)
        self.assertEqual(first.split("\t")[0], second.split("\t")[0])

    def test_valid_identity_rejects_wrong_boot_or_errors(self) -> None:
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:00Z",
            boot_id=self.boot,
            complete=True,
            errors=[],
        )
        self.assertTrue(self._valid_identity(self.census, self.boot))
        self.assertEqual(self._valid_identity(self.census, "other-boot"), "")

        write_census(
            self.census,
            captured_at="2026-08-01T00:00:01Z",
            boot_id=self.boot,
            complete=True,
            errors=[{"class": "permission"}],
        )
        self.assertEqual(self._valid_identity(self.census, self.boot), "")

        write_census(
            self.census,
            captured_at="2026-08-01T00:00:02Z",
            boot_id=self.boot,
            complete=False,
            errors=[],
        )
        self.assertEqual(self._valid_identity(self.census, self.boot), "")

    def test_preexisting_same_boot_snapshot_is_excluded(self) -> None:
        """Seed an old valid same-boot snapshot; it must not count as first/second."""
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:00Z",
            boot_id=self.boot,
            complete=True,
            errors=[],
            processes=[{"pid": 10, "comm": "old"}],
        )
        baseline = self._identity(self.census)
        self.assertTrue(baseline)
        # Same path still holds the pre-existing complete same-boot capture.
        candidate = self._valid_identity(self.census, self.boot)
        self.assertEqual(candidate, baseline)
        self.assertEqual(self._accept(baseline, "", candidate), "ignore")

        # First fresh capture after "restart" (new identity).
        self.census.unlink()
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:10Z",
            boot_id=self.boot,
            complete=True,
            errors=[],
            processes=[{"pid": 11, "comm": "fresh1"}],
        )
        first_cand = self._valid_identity(self.census, self.boot)
        self.assertNotEqual(first_cand, baseline)
        step1 = self._accept(baseline, "", first_cand)
        self.assertTrue(step1.startswith("first:"))
        first_id = step1[len("first:") :]
        self.assertEqual(first_id, first_cand)

        # Unchanged file is still first — not second.
        self.assertEqual(self._accept(baseline, first_id, first_cand), "ignore")

        # Second distinct subsequent capture.
        self.census.unlink()
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:20Z",
            boot_id=self.boot,
            complete=True,
            errors=[],
            processes=[{"pid": 12, "comm": "fresh2"}],
        )
        second_cand = self._valid_identity(self.census, self.boot)
        self.assertNotEqual(second_cand, baseline)
        self.assertNotEqual(second_cand, first_id)
        step2 = self._accept(baseline, first_id, second_cand)
        self.assertTrue(step2.startswith("second:"))
        self.assertEqual(step2[len("second:") :], second_cand)

        # Re-presenting the baseline after first is still ignored.
        self.assertEqual(self._accept(baseline, first_id, baseline), "ignore")

    def test_empty_baseline_accepts_first_valid(self) -> None:
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:00Z",
            boot_id=self.boot,
        )
        cand = self._valid_identity(self.census, self.boot)
        step = self._accept("", "", cand)
        self.assertEqual(step, f"first:{cand}")

    def test_poll_simulation_skips_seeded_old_snapshot(self) -> None:
        """End-to-end helper loop: old seed + two fresh writes → first/second only."""
        write_census(
            self.census,
            captured_at="2026-07-01T12:00:00Z",
            boot_id=self.boot,
            processes=[{"pid": 1}],
        )
        old_path = self.census
        fresh1 = self.root / "fresh1.json"
        fresh2 = self.root / "fresh2.json"
        write_census(
            fresh1,
            captured_at="2026-08-01T00:00:10Z",
            boot_id=self.boot,
            processes=[{"pid": 2}],
        )
        write_census(
            fresh2,
            captured_at="2026-08-01T00:00:20Z",
            boot_id=self.boot,
            processes=[{"pid": 3}],
        )

        script = _source_helpers_prefix() + textwrap.dedent(
            f"""\
            boot={self.boot!r}
            baseline=$(census_file_identity {old_path.as_posix()!r})
            first_identity=''
            second_identity=''
            # Simulate poll sequence: pre-existing path twice, then two fresh files.
            for path in \\
              {old_path.as_posix()!r} \\
              {old_path.as_posix()!r} \\
              {fresh1.as_posix()!r} \\
              {fresh1.as_posix()!r} \\
              {fresh2.as_posix()!r}
            do
              candidate=$(valid_census_capture_identity "$path" "$boot")
              step=$(accept_capture_step "$baseline" "$first_identity" "$candidate")
              case "$step" in
                first:*)
                  first_identity=${{step#first:}}
                  ;;
                second:*)
                  second_identity=${{step#second:}}
                  break
                  ;;
                ignore) ;;
                *)
                  printf 'bad-step:%s\\n' "$step" >&2
                  exit 2
                  ;;
              esac
            done
            [[ -n "$first_identity" ]] || {{ printf 'missing-first\\n' >&2; exit 3; }}
            [[ -n "$second_identity" ]] || {{ printf 'missing-second\\n' >&2; exit 4; }}
            [[ "$first_identity" != "$baseline" ]] || {{ printf 'first-is-baseline\\n' >&2; exit 5; }}
            [[ "$second_identity" != "$baseline" ]] || {{ printf 'second-is-baseline\\n' >&2; exit 6; }}
            [[ "$first_identity" != "$second_identity" ]] || {{ printf 'not-distinct\\n' >&2; exit 7; }}
            printf 'ok\\n'
            printf 'baseline_cap=%s\\n' "${{baseline%%$'\\t'*}}"
            printf 'first_cap=%s\\n' "${{first_identity%%$'\\t'*}}"
            printf 'second_cap=%s\\n' "${{second_identity%%$'\\t'*}}"
            """
        )
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(lines[0], "ok")
        self.assertIn("baseline_cap=2026-07-01T12:00:00Z", lines)
        self.assertIn("first_cap=2026-08-01T00:00:10Z", lines)
        self.assertIn("second_cap=2026-08-01T00:00:20Z", lines)


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


if __name__ == "__main__":
    unittest.main()
