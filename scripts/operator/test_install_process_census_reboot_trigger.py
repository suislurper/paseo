#!/usr/bin/env python3
"""Focused tests for install-process-census-reboot-trigger.sh helpers.

No real sudo. No real systemd. Temporary directories only.
Proves PCT-001 staging traverse/cleanup and PCT-002 quiesced baseline exclusion.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parent / "install-process-census-reboot-trigger.sh"
STAGING_PARENT = "/var/tmp"
STAGING_PREFIX = "paseo-process-census-install."
STAGED_NAMES = (
    "paseo-process-census.timer",
    "paseo-process-census.service",
    "paseo-process-census",
    "operator-fork.md",
)
# Exact production unit roots (paseo-process-census.service --root flags).
DEFAULT_CENSUS_ROOTS = (
    "/home/user/.paseo/worktrees",
    "/mnt/data/paseo-runtime",
    "/mnt/data/shab/.git",
    "/tmp",
)


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


def _fake_sudo_fn() -> str:
    """Sudo stand-in: drop -n, restore traverse on locked parents, run as self.

    cleanup_staging clears STAGING_DIR before rm, so unlock uses path parents
    (simulating root's ability to unlink through a mode-000 directory).
    """
    return textwrap.dedent(
        """\
        sudo() {
          local -a args=()
          local a parent
          for a in "$@"; do
            if [[ "$a" == "-n" ]]; then
              continue
            fi
            args+=("$a")
          done
          # Simulate elevated access: unlock parents of path operands.
          for a in "${args[@]}"; do
            [[ "$a" == /* ]] || continue
            parent=$(dirname -- "$a")
            if [[ -d "$parent" ]]; then
              chmod u+rwx -- "$parent" 2>/dev/null || true
            fi
            if [[ -d "$a" ]]; then
              chmod u+rwx -- "$a" 2>/dev/null || true
            fi
          done
          "${args[@]}"
        }
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
    roots: list | None = None,
    extra: dict | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "captured_at": captured_at,
        "boot_id": boot_id,
        "complete": complete,
        "errors": [] if errors is None else errors,
        "processes": [] if processes is None else processes,
        "roots": list(DEFAULT_CENSUS_ROOTS) if roots is None else list(roots),
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


class StagingTraverseAndCleanupTests(unittest.TestCase):
    """PCT-001: 0711 traverse + unconditional cleanup of exact known children."""

    def setUp(self) -> None:
        self.stage = Path(
            tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=STAGING_PARENT),
        )
        for name in STAGED_NAMES:
            (self.stage / name).write_text(f"payload:{name}\n", encoding="utf-8")
            if name == "paseo-process-census":
                (self.stage / name).chmod(0o755)
            else:
                (self.stage / name).chmod(0o644)

    def tearDown(self) -> None:
        # Best-effort residual cleanup if a test failed mid-way.
        if self.stage.exists():
            try:
                self.stage.chmod(0o700)
            except OSError:
                pass
            for name in STAGED_NAMES:
                p = self.stage / name
                if p.exists() or p.is_symlink():
                    p.unlink(missing_ok=True)
            try:
                self.stage.rmdir()
            except OSError:
                pass

    def test_mode_0711_allows_unprivileged_traverse_and_hash(self) -> None:
        """0700-style lock blocks child open; 0711 restores traverse for verify."""
        # Lock down like root mktemp 0700 would for a non-owner — use 000 so even
        # the owner cannot open children (deterministic without real root).
        self.stage.chmod(0o000)
        blocked = _bash(
            textwrap.dedent(
                f"""\
                set -euo pipefail
                if sha256sum -- {str(self.stage / STAGED_NAMES[0])!r} >/dev/null 2>&1; then
                  printf 'open\\n'
                else
                  printf 'blocked\\n'
                fi
                """
            )
        )
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(blocked.stdout.strip(), "blocked")

        self.stage.chmod(0o711)
        mode = stat.S_IMODE(self.stage.stat().st_mode)
        self.assertEqual(mode, 0o711)

        opened = _bash(
            textwrap.dedent(
                f"""\
                set -euo pipefail
                sha256sum -- {str(self.stage / STAGED_NAMES[0])!r} | awk '{{print $1}}'
                """
            )
        )
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertRegex(opened.stdout.strip(), r"^[0-9a-f]{64}$")

    def test_cleanup_unconditional_rm_removes_children_when_dir_not_traversable(self) -> None:
        """Existence tests would miss children under locked dir; unconditional sudo rm must not."""
        # Prove unprivileged existence checks cannot see children under 000.
        self.stage.chmod(0o000)
        exists_probe = _bash(
            textwrap.dedent(
                f"""\
                set -euo pipefail
                d={str(self.stage)!r}
                seen=0
                for f in \\
                  paseo-process-census.timer \\
                  paseo-process-census.service \\
                  paseo-process-census \\
                  operator-fork.md
                do
                  if [[ -e "$d/$f" || -L "$d/$f" ]]; then
                    seen=$((seen + 1))
                  fi
                done
                printf '%s\\n' "$seen"
                """
            )
        )
        self.assertEqual(exists_probe.returncode, 0, exists_probe.stderr)
        self.assertEqual(exists_probe.stdout.strip(), "0")

        # cleanup_staging with fake sudo must still clear all four + rmdir.
        script = (
            _source_helpers_prefix()
            + _fake_sudo_fn()
            + textwrap.dedent(
                f"""\
                STAGING_DIR={str(self.stage)!r}
                cleanup_staging
                if [[ -e {str(self.stage)!r} ]]; then
                  printf 'residue-dir\\n' >&2
                  exit 2
                fi
                printf 'clean\\n'
                """
            )
        )
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(proc.stdout.strip(), "clean")
        self.assertFalse(self.stage.exists())

    def test_cleanup_noop_on_invalid_or_unset_staging(self) -> None:
        script = (
            _source_helpers_prefix()
            + _fake_sudo_fn()
            + textwrap.dedent(
                """\
                STAGING_DIR=
                cleanup_staging
                STAGING_DIR=/tmp/paseo-process-census-install.evil
                cleanup_staging
                STAGING_DIR=/var/tmp/not-the-prefix.xyz
                cleanup_staging
                printf 'ok\\n'
                """
            )
        )
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")
        # Original fixture still present (invalid paths must not delete it).
        self.stage.chmod(0o700)
        self.assertTrue(self.stage.is_dir())
        for name in STAGED_NAMES:
            self.assertTrue((self.stage / name).is_file())

    def test_cleanup_issues_exact_four_rm_and_rmdir_no_glob(self) -> None:
        """Record fake-sudo argv: four exact rm -f paths + one exact rmdir."""
        log = self.stage.parent / f"{self.stage.name}.sudo-log"
        if log.exists():
            log.unlink()
        script = (
            _source_helpers_prefix()
            + textwrap.dedent(
                f"""\
                SUDO_LOG={log.as_posix()!r}
                sudo() {{
                  local -a args=()
                  local a parent
                  for a in "$@"; do
                    [[ "$a" == "-n" ]] && continue
                    args+=("$a")
                  done
                  {{
                    printf 'CMD'
                    printf '\\t%s' "${{args[@]}}"
                    printf '\\n'
                  }} >>"$SUDO_LOG"
                  for a in "${{args[@]}}"; do
                    [[ "$a" == /* ]] || continue
                    parent=$(dirname -- "$a")
                    if [[ -d "$parent" ]]; then
                      chmod u+rwx -- "$parent" 2>/dev/null || true
                    fi
                    if [[ -d "$a" ]]; then
                      chmod u+rwx -- "$a" 2>/dev/null || true
                    fi
                  done
                  "${{args[@]}}"
                }}
                STAGING_DIR={str(self.stage)!r}
                cleanup_staging
                printf 'ok\\n'
                """
            )
        )
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertFalse(self.stage.exists())
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 5, lines)
        rm_lines = [ln for ln in lines if ln.startswith("CMD\trm\t")]
        rmdir_lines = [ln for ln in lines if ln.startswith("CMD\trmdir\t")]
        self.assertEqual(len(rm_lines), 4, lines)
        self.assertEqual(len(rmdir_lines), 1, lines)
        for name in STAGED_NAMES:
            expected = f"CMD\trm\t-f\t--\t{self.stage / name}"
            self.assertIn(expected, lines)
        self.assertEqual(rmdir_lines[0], f"CMD\trmdir\t--\t{self.stage}")
        joined = "\n".join(lines)
        self.assertNotIn("*", joined)
        self.assertNotIn("rm -rf", joined)
        self.assertNotIn("rm\t-rf", joined)
        log.unlink(missing_ok=True)

    def test_stage_create_chmod_verify_cleanup_integration_with_fakes(self) -> None:
        """Integration-style: mktemp→0711 verify→stage files→hash→cleanup, no residue."""
        stage_holder = Path(
            tempfile.mkdtemp(prefix=f"{STAGING_PREFIX}holder-", dir=STAGING_PARENT),
        )
        try:
            script = (
                _source_helpers_prefix()
                + _fake_sudo_fn()
                + textwrap.dedent(
                    f"""\
                    # Use sourced STAGING_PARENT (readonly); do not reassign.
                    STAGING_DIR=$(mktemp -d -p "$STAGING_PARENT" "${{STAGING_NAME_PREFIX}}XXXXXX")
                    is_valid_staging_dir "$STAGING_DIR" || {{
                      printf 'bad-stage:%s\\n' "$STAGING_DIR" >&2
                      exit 2
                    }}
                    # Simulate mktemp 0700 then fix to reviewed 0711.
                    chmod 0700 -- "$STAGING_DIR"
                    chmod -- "$STAGING_DIR_MODE" "$STAGING_DIR"
                    mode=$(stat -c '%a' -- "$STAGING_DIR")
                    [[ "$mode" == "711" ]] || {{
                      printf 'mode-want-711-got-%s\\n' "$mode" >&2
                      exit 3
                    }}
                    # Stage four known files (owner is test user; modes match install).
                    printf 'timer\\n' >"$STAGING_DIR/$STAGED_TIMER_NAME"
                    printf 'service\\n' >"$STAGING_DIR/$STAGED_SERVICE_NAME"
                    printf 'helper\\n' >"$STAGING_DIR/$STAGED_HELPER_NAME"
                    printf 'doc\\n' >"$STAGING_DIR/$STAGED_DOC_NAME"
                    chmod 0644 -- "$STAGING_DIR/$STAGED_TIMER_NAME" \\
                      "$STAGING_DIR/$STAGED_SERVICE_NAME" \\
                      "$STAGING_DIR/$STAGED_DOC_NAME"
                    chmod 0755 -- "$STAGING_DIR/$STAGED_HELPER_NAME"
                    # Unprivileged traverse+hash must succeed under 0711.
                    for f in \\
                      "$STAGED_TIMER_NAME" \\
                      "$STAGED_SERVICE_NAME" \\
                      "$STAGED_HELPER_NAME" \\
                      "$STAGED_DOC_NAME"
                    do
                      sha256sum -- "$STAGING_DIR/$f" >/dev/null
                      got=$(stat -c '%a' -- "$STAGING_DIR/$f")
                      case "$f" in
                        "$STAGED_HELPER_NAME")
                          [[ "$got" == "755" ]] || exit 4
                          ;;
                        *)
                          [[ "$got" == "644" ]] || exit 5
                          ;;
                      esac
                    done
                    # Record path then cleanup; must leave no residue.
                    printf '%s\\n' "$STAGING_DIR" >{stage_holder.as_posix()!r}/path.txt
                    cleanup_staging
                    [[ ! -e $(cat {stage_holder.as_posix()!r}/path.txt) ]] || {{
                      printf 'residue\\n' >&2
                      exit 6
                    }}
                    printf 'ok\\n'
                    """
                )
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stdout.strip(), "ok")
            path_file = stage_holder / "path.txt"
            self.assertTrue(path_file.is_file())
            staged = Path(path_file.read_text(encoding="utf-8").strip())
            self.assertFalse(staged.exists())
        finally:
            # holder only; staged path already cleaned
            for p in stage_holder.iterdir():
                p.unlink(missing_ok=True)
            stage_holder.rmdir()


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

    def test_valid_identity_requires_exact_four_roots(self) -> None:
        """Exact set of service roots accepted; missing/subset/duplicate/extra rejected."""
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:00Z",
            boot_id=self.boot,
            roots=list(DEFAULT_CENSUS_ROOTS),
        )
        self.assertTrue(self._valid_identity(self.census, self.boot))

        # Order-independent: permutation of the same four still accepted.
        perm = [
            DEFAULT_CENSUS_ROOTS[2],
            DEFAULT_CENSUS_ROOTS[0],
            DEFAULT_CENSUS_ROOTS[3],
            DEFAULT_CENSUS_ROOTS[1],
        ]
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:01Z",
            boot_id=self.boot,
            roots=perm,
        )
        self.assertTrue(self._valid_identity(self.census, self.boot))

        # Missing / subset.
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:02Z",
            boot_id=self.boot,
            roots=list(DEFAULT_CENSUS_ROOTS[:3]),
        )
        self.assertEqual(self._valid_identity(self.census, self.boot), "")

        # Duplicate (length 4 but not unique; not the exact set).
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:03Z",
            boot_id=self.boot,
            roots=[
                DEFAULT_CENSUS_ROOTS[0],
                DEFAULT_CENSUS_ROOTS[1],
                DEFAULT_CENSUS_ROOTS[2],
                DEFAULT_CENSUS_ROOTS[0],
            ],
        )
        self.assertEqual(self._valid_identity(self.census, self.boot), "")

        # Extra root.
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:04Z",
            boot_id=self.boot,
            roots=[*DEFAULT_CENSUS_ROOTS, "/var/extra"],
        )
        self.assertEqual(self._valid_identity(self.census, self.boot), "")

        # Wrong set (same length, different member).
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:05Z",
            boot_id=self.boot,
            roots=[
                DEFAULT_CENSUS_ROOTS[0],
                DEFAULT_CENSUS_ROOTS[1],
                DEFAULT_CENSUS_ROOTS[2],
                "/var/wrong",
            ],
        )
        self.assertEqual(self._valid_identity(self.census, self.boot), "")

        # Missing roots key entirely.
        payload = {
            "captured_at": "2026-08-01T00:00:06Z",
            "boot_id": self.boot,
            "complete": True,
            "errors": [],
            "processes": [],
        }
        self.census.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(self._valid_identity(self.census, self.boot), "")

    def test_valid_identity_rejects_malformed_unreadable(self) -> None:
        self.census.parent.mkdir(parents=True, exist_ok=True)
        self.census.write_text("{not-json\n", encoding="utf-8")
        self.assertEqual(self._valid_identity(self.census, self.boot), "")

        self.census.write_text("", encoding="utf-8")
        self.assertEqual(self._valid_identity(self.census, self.boot), "")

        # Unreadable: mode 000 (owner also blocked for open in this process).
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:00Z",
            boot_id=self.boot,
        )
        self.census.chmod(0o000)
        try:
            self.assertEqual(self._valid_identity(self.census, self.boot), "")
        finally:
            self.census.chmod(0o644)

    def test_atomic_path_replacement_does_not_mix_generations(self) -> None:
        """Observation is open-once: path swap after open cannot mix validate/identity gens.

        Former bug: jq-validate path (gen A) then re-open path for identity (gen B).
        Fixed path holds one fd, copies bytes, validates+hashes the private copy only.
        """
        gen_a = self.root / "gen-a.json"
        gen_b = self.root / "gen-b.json"
        path = self.root / "live.json"
        write_census(
            gen_a,
            captured_at="2026-08-01T00:00:10Z",
            boot_id=self.boot,
            complete=True,
            errors=[],
            processes=[{"pid": 1, "comm": "gen-a"}],
        )
        # Gen B is incomplete / errors — must never be accepted as a validated identity.
        write_census(
            gen_b,
            captured_at="2026-08-01T00:00:99Z",
            boot_id=self.boot,
            complete=False,
            errors=[{"class": "permission"}],
            processes=[{"pid": 2, "comm": "gen-b"}],
        )
        # Also a complete-looking B with wrong roots for a second swap case.
        gen_b_bad_roots = self.root / "gen-b-bad-roots.json"
        write_census(
            gen_b_bad_roots,
            captured_at="2026-08-01T00:00:99Z",
            boot_id=self.boot,
            complete=True,
            errors=[],
            roots=["/tmp"],
            processes=[{"pid": 3, "comm": "gen-b-roots"}],
        )

        # 1) After open+copy of A, swap path to incomplete B; private copy stays A.
        script = _source_helpers_prefix() + textwrap.dedent(
            f"""\
            set -euo pipefail
            path={path.as_posix()!r}
            gen_a={gen_a.as_posix()!r}
            gen_b={gen_b.as_posix()!r}
            boot={self.boot!r}
            cp -f -- "$gen_a" "$path"
            obs=$(census_open_snapshot_copy "$path")
            [[ -n "$obs" ]] || {{ printf 'open-failed\\n' >&2; exit 2; }}
            st=${{obs%%$'\\t'*}}
            tmp=${{obs#*$'\\t'}}
            # Atomic-ish path replacement while private copy is held.
            cp -f -- "$gen_b" "$path"
            # Re-reading the path must see incomplete B (would be wrongly "valid" under TOCTOU).
            if jq -e --arg boot "$boot" '
                .complete == true and
                (.errors | type == "array") and
                (.errors | length) == 0 and
                .boot_id == $boot
              ' "$path" >/dev/null 2>&1; then
              printf 'path-still-valid\\n' >&2
              rm -f -- "$tmp"
              exit 3
            fi
            # Private copy must still be complete gen A — validate+hash the copy only.
            if ! jq -e --arg boot "$boot" --argjson want "$CENSUS_REQUIRED_ROOTS_JSON" '
                .complete == true and
                (.errors | length) == 0 and
                .boot_id == $boot and
                (.captured_at == "2026-08-01T00:00:10Z") and
                ((.roots | sort) == ($want | sort))
              ' "$tmp" >/dev/null 2>&1; then
              printf 'copy-invalid\\n' >&2
              rm -f -- "$tmp"
              exit 4
            fi
            sha=$(file_sha256 "$tmp")
            cap=$(jq -r '.captured_at' "$tmp")
            rm -f -- "$tmp"
            # Full helper on path now (B) must reject incomplete/error bytes.
            after=$(valid_census_capture_identity "$path" "$boot")
            [[ -z "$after" ]] || {{
              printf 'accepted-b:%s\\n' "$after" >&2
              exit 5
            }}
            printf 'ok\\n'
            printf 'held_cap=%s\\n' "$cap"
            printf 'held_st=%s\\n' "$st"
            printf 'held_sha=%s\\n' "$sha"
            """
        )
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(lines[0], "ok")
        self.assertIn("held_cap=2026-08-01T00:00:10Z", lines)

        # 2) Full helper never mixes: pure A accepted; pure B (bad roots) rejected.
        shutil.copy2(gen_a, path)
        id_a = self._valid_identity(path, self.boot)
        self.assertTrue(id_a)
        self.assertEqual(id_a.split("\t")[0], "2026-08-01T00:00:10Z")
        shutil.copy2(gen_b_bad_roots, path)
        self.assertEqual(self._valid_identity(path, self.boot), "")
        shutil.copy2(gen_b, path)
        self.assertEqual(self._valid_identity(path, self.boot), "")

    def test_second_requires_strictly_later_captured_at(self) -> None:
        """Distinct identity alone is not enough; second captured_at must be strictly later."""
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:10Z",
            boot_id=self.boot,
            processes=[{"pid": 1}],
        )
        first = self._valid_identity(self.census, self.boot)
        self.assertTrue(first)

        # Equal captured_at, different body/inode → ignore (not second).
        self.census.unlink()
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:10Z",
            boot_id=self.boot,
            processes=[{"pid": 2}],
        )
        equal_cand = self._valid_identity(self.census, self.boot)
        self.assertTrue(equal_cand)
        self.assertNotEqual(equal_cand, first)
        self.assertEqual(self._accept("", first, equal_cand), "ignore")

        # Earlier captured_at → ignore.
        self.census.unlink()
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:05Z",
            boot_id=self.boot,
            processes=[{"pid": 3}],
        )
        earlier = self._valid_identity(self.census, self.boot)
        self.assertTrue(earlier)
        self.assertEqual(self._accept("", first, earlier), "ignore")

        # Strictly later + distinct → second.
        self.census.unlink()
        write_census(
            self.census,
            captured_at="2026-08-01T00:00:20Z",
            boot_id=self.boot,
            processes=[{"pid": 4}],
        )
        later = self._valid_identity(self.census, self.boot)
        self.assertTrue(later)
        step = self._accept("", first, later)
        self.assertTrue(step.startswith("second:"))
        self.assertEqual(step[len("second:") :], later)

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

    def test_capture_before_quiescence_boundary_is_excluded(self) -> None:
        """PCT-002: in-flight capture that lands before the quiesced baseline is not first/second.

        Sequence models: stop timer → in-flight producer finishes → service inactive →
        record baseline → start timer → only later replacements count.
        """
        pre = self.root / "pre.json"
        inflight = self.root / "inflight.json"
        post1 = self.root / "post1.json"
        post2 = self.root / "post2.json"
        write_census(
            pre,
            captured_at="2026-08-01T00:00:00Z",
            boot_id=self.boot,
            processes=[{"pid": 1, "comm": "pre"}],
        )
        write_census(
            inflight,
            captured_at="2026-08-01T00:00:05Z",
            boot_id=self.boot,
            processes=[{"pid": 2, "comm": "inflight"}],
        )
        write_census(
            post1,
            captured_at="2026-08-01T00:00:15Z",
            boot_id=self.boot,
            processes=[{"pid": 3, "comm": "post1"}],
        )
        write_census(
            post2,
            captured_at="2026-08-01T00:00:25Z",
            boot_id=self.boot,
            processes=[{"pid": 4, "comm": "post2"}],
        )

        script = _source_helpers_prefix() + textwrap.dedent(
            f"""\
            boot={self.boot!r}
            # Wrong order (pre-restart race): baseline before in-flight completes.
            early_baseline=$(census_file_identity {pre.as_posix()!r})
            inflight_id=$(valid_census_capture_identity {inflight.as_posix()!r} "$boot")
            wrong_step=$(accept_capture_step "$early_baseline" "" "$inflight_id")
            # Buggy path would accept the in-flight write as first.
            [[ "$wrong_step" == first:* ]] || {{
              printf 'expected-wrong-first\\n' >&2
              exit 2
            }}

            # Correct order: quiesce, then baseline includes the completed in-flight write.
            baseline=$(census_file_identity {inflight.as_posix()!r})
            [[ -n "$baseline" ]] || {{ printf 'empty-baseline\\n' >&2; exit 3; }}
            # Same in-flight identity after boundary must be ignored.
            step0=$(accept_capture_step "$baseline" "" "$inflight_id")
            [[ "$step0" == ignore ]] || {{
              printf 'inflight-not-ignored:%s\\n' "$step0" >&2
              exit 4
            }}

            first_identity=''
            second_identity=''
            for path in \\
              {inflight.as_posix()!r} \\
              {post1.as_posix()!r} \\
              {post1.as_posix()!r} \\
              {post2.as_posix()!r}
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
                  exit 5
                  ;;
              esac
            done
            [[ -n "$first_identity" && -n "$second_identity" ]] || {{
              printf 'missing-captures\\n' >&2
              exit 6
            }}
            [[ "$first_identity" != "$baseline" ]] || {{
              printf 'first-is-baseline\\n' >&2
              exit 7
            }}
            [[ "$first_identity" != "$inflight_id" ]] || {{
              printf 'first-is-inflight\\n' >&2
              exit 8
            }}
            [[ "$second_identity" != "$inflight_id" ]] || {{
              printf 'second-is-inflight\\n' >&2
              exit 9
            }}
            printf 'ok\\n'
            printf 'first_cap=%s\\n' "${{first_identity%%$'\\t'*}}"
            printf 'second_cap=%s\\n' "${{second_identity%%$'\\t'*}}"
            """
        )
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(lines[0], "ok")
        self.assertIn("first_cap=2026-08-01T00:00:15Z", lines)
        self.assertIn("second_cap=2026-08-01T00:00:25Z", lines)


class WaitServiceInactiveTests(unittest.TestCase):
    """PCT-002: wait_service_inactive fail-closed behaviour with fake systemctl."""

    def test_wait_accepts_inactive_and_dead(self) -> None:
        script = _source_helpers_prefix() + textwrap.dedent(
            """\
            systemctl() {
              if [[ "$1" == "is-active" ]]; then
                printf 'inactive\\n'
                return 0
              fi
              printf 'unexpected systemctl: %s\\n' "$*" >&2
              return 1
            }
            SECONDS=0
            wait_service_inactive paseo-process-census.service $((SECONDS + 5))
            systemctl() {
              if [[ "$1" == "is-active" ]]; then
                printf 'dead\\n'
                return 0
              fi
              return 1
            }
            wait_service_inactive paseo-process-census.service $((SECONDS + 5))
            printf 'ok\\n'
            """
        )
        proc = _bash(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")

    def test_wait_fails_closed_on_failed_state(self) -> None:
        script = _source_helpers_prefix() + textwrap.dedent(
            """\
            systemctl() {
              if [[ "$1" == "is-active" ]]; then
                printf 'failed\\n'
                return 0
              fi
              return 1
            }
            SECONDS=0
            if wait_service_inactive paseo-process-census.service $((SECONDS + 5)); then
              printf 'unexpected-success\\n'
              exit 2
            else
              rc=$?
              printf 'failed-closed rc=%s\\n' "$rc"
            fi
            """
        )
        proc = _bash(script)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("failed state", proc.stderr)

    def test_wait_fails_closed_on_timeout_while_active(self) -> None:
        script = _source_helpers_prefix() + textwrap.dedent(
            """\
            systemctl() {
              if [[ "$1" == "is-active" ]]; then
                printf 'active\\n'
                return 0
              fi
              return 1
            }
            # Freeze SECONDS so the deadline is already past on entry to the loop end.
            SECONDS=100
            if wait_service_inactive paseo-process-census.service 100; then
              printf 'unexpected-success\\n'
              exit 2
            fi
            """
        )
        proc = _bash(script)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("did not become inactive", proc.stderr)

    def test_activation_sequence_orders_stop_wait_baseline_start(self) -> None:
        """Deterministic fake sequence: in-flight before baseline excluded; only post counts."""
        tmp = tempfile.TemporaryDirectory(prefix="pct-seq-")
        root = Path(tmp.name)
        try:
            census = root / "process-census.json"
            log = root / "sys.log"
            phase_file = root / "phase"
            stop_file = root / "timer-stopped"
            inflight_body = root / "inflight-body.json"
            # Pre-seed a capture that will be replaced mid-quiesce.
            write_census(
                census,
                captured_at="2026-08-01T00:00:00Z",
                boot_id="boot-seq-1",
                processes=[{"pid": 1}],
            )
            write_census(
                inflight_body,
                captured_at="2026-08-01T00:00:30Z",
                boot_id="boot-seq-1",
                processes=[{"pid": 2, "comm": "inflight"}],
            )
            post1 = root / "post1.json"
            post2 = root / "post2.json"
            write_census(
                post1,
                captured_at="2026-08-01T00:01:00Z",
                boot_id="boot-seq-1",
                processes=[{"pid": 10}],
            )
            write_census(
                post2,
                captured_at="2026-08-01T00:02:00Z",
                boot_id="boot-seq-1",
                processes=[{"pid": 11}],
            )
            phase_file.write_text("0\n", encoding="utf-8")
            stop_file.write_text("0\n", encoding="utf-8")
            # Phase/stop files: command substitutions run systemctl in a subshell,
            # so state must live on disk (not shell variables).
            script = _source_helpers_prefix() + textwrap.dedent(
                f"""\
                LOG={log.as_posix()!r}
                CENSUS={census.as_posix()!r}
                POST1={post1.as_posix()!r}
                POST2={post2.as_posix()!r}
                PHASE_FILE={phase_file.as_posix()!r}
                STOP_FILE={stop_file.as_posix()!r}
                INFLIGHT_BODY={inflight_body.as_posix()!r}
                boot=boot-seq-1

                systemctl() {{
                  printf '%s\\n' "$*" >>"$LOG"
                  case "$1" in
                    stop)
                      [[ "$2" == "--" && "$3" == "paseo-process-census.timer" ]] || return 1
                      printf '1\\n' >"$STOP_FILE"
                      return 0
                      ;;
                    is-active)
                      unit=${{3:-$2}}
                      if [[ "$unit" == "paseo-process-census.service" ]]; then
                        phase=$(cat "$PHASE_FILE")
                        stopped=$(cat "$STOP_FILE")
                        if [[ "$phase" == "0" ]]; then
                          [[ "$stopped" == "1" ]] || {{
                            printf 'inflight-before-stop\\n' >&2
                            return 1
                          }}
                          # In-flight producer finishes; still active this poll.
                          cp -- "$INFLIGHT_BODY" "$CENSUS"
                          printf '1\\n' >"$PHASE_FILE"
                          printf 'active\\n'
                          return 0
                        fi
                        printf 'inactive\\n'
                        return 0
                      fi
                      if [[ "$unit" == "paseo-process-census.timer" ]]; then
                        printf 'active\\n'
                        return 0
                      fi
                      ;;
                    start|enable|daemon-reload|is-enabled|show)
                      return 0
                      ;;
                  esac
                  return 0
                }}

                # --- activation fragment under test (mirrors installer order) ---
                systemctl stop -- paseo-process-census.timer
                SECONDS=0
                wait_service_inactive paseo-process-census.service $((SECONDS + 10))
                baseline=$(census_file_identity "$CENSUS")
                [[ -n "$baseline" ]] || {{ printf 'empty-baseline\\n' >&2; exit 2; }}
                systemctl enable -- paseo-process-census.timer
                systemctl start -- paseo-process-census.timer

                first_identity=''
                second_identity=''
                for path in "$CENSUS" "$POST1" "$POST2"; do
                  candidate=$(valid_census_capture_identity "$path" "$boot")
                  step=$(accept_capture_step "$baseline" "$first_identity" "$candidate")
                  case "$step" in
                    first:*) first_identity=${{step#first:}} ;;
                    second:*) second_identity=${{step#second:}}; break ;;
                    ignore) ;;
                    *) printf 'bad:%s\\n' "$step" >&2; exit 3 ;;
                  esac
                done
                [[ -n "$first_identity" && -n "$second_identity" ]] || exit 4
                # Baseline must be the in-flight capture (00:00:30), not pre (00:00:00).
                [[ "${{baseline%%$'\\t'*}}" == "2026-08-01T00:00:30Z" ]] || {{
                  printf 'baseline_cap=%s\\n' "${{baseline%%$'\\t'*}}" >&2
                  exit 5
                }}
                [[ "${{first_identity%%$'\\t'*}}" == "2026-08-01T00:01:00Z" ]] || exit 6
                [[ "${{second_identity%%$'\\t'*}}" == "2026-08-01T00:02:00Z" ]] || exit 7
                # Must never stop/kill/restart the service unit.
                if grep -E '(^|[[:space:]])(stop|kill|restart)([[:space:]].*)?paseo-process-census\\.service' "$LOG"; then
                  printf 'service-mutated\\n' >&2
                  exit 8
                fi
                if ! grep -q 'stop -- paseo-process-census.timer' "$LOG"; then
                  printf 'timer-not-stopped\\n' >&2
                  exit 9
                fi
                printf 'ok\\n'
                """
            )
            proc = _bash(script)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertEqual(proc.stdout.strip().splitlines()[0], "ok")
        finally:
            tmp.cleanup()


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

    def test_surface_mentions_quiesce_and_staging_mode(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("STAGING_DIR_MODE=0711", text)
        self.assertIn("wait_service_inactive", text)
        self.assertIn("SERVICE_UNIT=paseo-process-census.service", text)
        # Must not restart/kill the census service for quiescence.
        self.assertNotIn('systemctl restart -- "$SERVICE_UNIT"', text)
        self.assertNotIn('systemctl stop -- "$SERVICE_UNIT"', text)
        self.assertNotIn('systemctl kill', text)


if __name__ == "__main__":
    unittest.main()
