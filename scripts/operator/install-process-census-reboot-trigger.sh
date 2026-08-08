#!/usr/bin/env bash
# Non-interactive operator installer for the process-census OnActiveSec timer fix.
# Fail-closed: verifies source SHA/ancestry/fork remote before any privilege use.
# Restarts only paseo-process-census.timer — never Paseo or other services.
#
# PCT-001: after unprivileged preflight, copy reviewed inputs into a unique
# root-owned staging dir, re-verify hashes/modes there, install only from stage,
# and re-verify destinations before daemon-reload/enable/restart.
# PCT-002: record any pre-existing census snapshot identity, then require two
# distinct complete same-boot empty-error captures strictly after restart.
set -euo pipefail

readonly SOURCE_COMMIT=83bf0839c16ed73191f097ccd905aa81ee6acd14
readonly EXPECTED_FORK_BASE='https://github.com/suislurper/paseo'
readonly EXPECTED_TIMER_SHA=c6ae90d72818c12fff69ce6e120f21bfb25cea564d3bedbd314fe5588b2dc6d0
readonly EXPECTED_SERVICE_SHA=0eb3a9e3bb537cb2ede2cede371784e3c7882ccf623e2c35baa4cf37cc5919d6
readonly EXPECTED_HELPER_SHA=6241954df045e75cbd669d3136718b83395fb9a71f218bd58884da56a33daf92
readonly EXPECTED_DOC_SHA=fb4e16756a0b1b5bf10a5122f392b73df847f58e0633bd48f109e8891b3dc6f3

readonly DEST_HELPER=/usr/local/libexec/paseo-process-census
readonly DEST_SERVICE=/etc/systemd/system/paseo-process-census.service
readonly DEST_TIMER=/etc/systemd/system/paseo-process-census.timer
readonly DEST_DOC_DIR=/usr/local/share/doc/paseo
readonly DEST_DOC="${DEST_DOC_DIR}/operator-fork.md"
readonly CENSUS_OUT=/run/paseo/process-census.json
readonly TIMER_UNIT=paseo-process-census.timer
readonly BACKUP_PARENT=/mnt/data/paseo-runtime/artifacts/operator-install/backups

# Narrow, explicit staging parent/prefix. Cleanup validates both before removal.
readonly STAGING_PARENT=/var/tmp
readonly STAGING_NAME_PREFIX=paseo-process-census-install.
readonly STAGED_TIMER_NAME=paseo-process-census.timer
readonly STAGED_SERVICE_NAME=paseo-process-census.service
readonly STAGED_HELPER_NAME=paseo-process-census
readonly STAGED_DOC_NAME=operator-fork.md

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

file_sha256() {
  sha256sum -- "$1" | awk '{print $1}'
}

assert_sha() {
  local path=$1 expected=$2 actual
  actual=$(file_sha256 "$path")
  [[ "$actual" == "$expected" ]] || die "sha256 mismatch for $path: got $actual want $expected"
}

assert_owner_mode() {
  local path=$1 want=$2 got
  got=$(stat -c '%U:%G:%a' -- "$path")
  [[ "$got" == "$want" ]] || die "owner/mode mismatch for $path: got $got want $want"
}

# True when $1 is exactly one directory under STAGING_PARENT whose basename
# starts with STAGING_NAME_PREFIX. Rejects relative paths, .., and extra depth.
is_valid_staging_dir() {
  local d=$1 base parent
  [[ -n "$d" ]] || return 1
  [[ "$d" == /* ]] || return 1
  # Reject any path component that is ".." (string check; paths are absolute).
  [[ "$d" != *'/..'* && "$d" != *'/../'* && "$d" != '..'* ]] || return 1
  parent=$(dirname -- "$d")
  base=$(basename -- "$d")
  [[ "$parent" == "$STAGING_PARENT" ]] || return 1
  [[ "$base" == "${STAGING_NAME_PREFIX}"* ]] || return 1
  [[ "$base" != *'/'* && "$base" != *'*'* ]] || return 1
  return 0
}

# Remove only the four known staged basenames, then rmdir. No globs, no rm -rf.
# Safe no-op when STAGING_DIR is unset/invalid. Uses sudo -n only.
cleanup_staging() {
  local d=${STAGING_DIR:-}
  local f
  STAGING_DIR=
  [[ -n "$d" ]] || return 0
  is_valid_staging_dir "$d" || return 0
  [[ -d "$d" ]] || return 0
  for f in \
    "$STAGED_TIMER_NAME" \
    "$STAGED_SERVICE_NAME" \
    "$STAGED_HELPER_NAME" \
    "$STAGED_DOC_NAME"; do
    # Only remove the exact known basenames under the validated dir.
    if [[ -e "$d/$f" || -L "$d/$f" ]]; then
      sudo -n rm -f -- "$d/$f" || true
    fi
  done
  # Exact dir removal only when empty (no broad recursive delete).
  sudo -n rmdir -- "$d" 2>/dev/null || true
}

# Robust snapshot identity: captured_at + device/inode/size/mtime + content hash.
# Replacement that preserves captured_at still changes inode and/or hash.
# Prints empty string when the path is missing or unreadable for identity.
census_file_identity() {
  local path=$1
  local st sha cap
  if [[ ! -f "$path" ]]; then
    printf ''
    return 0
  fi
  st=$(stat -c '%d:%i:%s:%Y' -- "$path" 2>/dev/null) || {
    printf ''
    return 0
  }
  sha=$(file_sha256 "$path" 2>/dev/null) || {
    printf ''
    return 0
  }
  cap=$(
    jq -r 'if ((.captured_at | type) == "string") then .captured_at else empty end' \
      "$path" 2>/dev/null || true
  )
  printf '%s\t%s\t%s' "${cap}" "$st" "$sha"
}

# Identity of a complete, same-boot, empty-errors capture; empty if invalid.
valid_census_capture_identity() {
  local path=$1
  local boot=$2
  if [[ ! -f "$path" ]]; then
    printf ''
    return 0
  fi
  if ! jq -e --arg boot "$boot" '
      .complete == true and
      (.errors | type == "array") and
      (.errors | length) == 0 and
      .boot_id == $boot and
      (.captured_at | type == "string") and
      (.captured_at | length) > 0
    ' "$path" >/dev/null 2>&1; then
    printf ''
    return 0
  fi
  census_file_identity "$path"
}

# Classify a candidate identity relative to baseline and first post-restart id.
# Prints: ignore | first:<id> | second:<id>
# Pre-existing baseline is never accepted as first/second.
accept_capture_step() {
  local baseline=$1
  local first=$2
  local candidate=$3
  if [[ -z "$candidate" ]]; then
    printf 'ignore\n'
    return 0
  fi
  if [[ -n "$baseline" && "$candidate" == "$baseline" ]]; then
    printf 'ignore\n'
    return 0
  fi
  if [[ -z "$first" ]]; then
    printf 'first:%s\n' "$candidate"
    return 0
  fi
  if [[ "$candidate" == "$first" ]]; then
    printf 'ignore\n'
    return 0
  fi
  printf 'second:%s\n' "$candidate"
}

# ---- main install (skipped when sourced for unit tests) ----------------------

process_census_install_main() {
  require_cmd git
  require_cmd sha256sum
  require_cmd install
  require_cmd systemctl
  require_cmd jq
  require_cmd sudo
  require_cmd date
  require_cmd awk
  require_cmd mkdir
  require_cmd cp
  require_cmd stat
  require_cmd cat
  require_cmd sleep
  require_cmd mktemp
  require_cmd dirname
  require_cmd basename
  require_cmd rmdir
  require_cmd rm
  require_cmd tr

  local script_path script_dir source_root
  script_path=$(readlink -f -- "${BASH_SOURCE[0]}")
  script_dir=$(dirname -- "$script_path")
  # scripts/operator/ -> repo root
  source_root=$(cd -- "$script_dir/../.." && pwd -P)

  [[ -d "$source_root/.git" || -f "$source_root/.git" ]] ||
    die "cannot derive git checkout from $script_path (root=$source_root)"

  local timer_src service_src helper_src doc_src
  timer_src="$source_root/scripts/operator/systemd/paseo-process-census.timer"
  service_src="$source_root/scripts/operator/systemd/paseo-process-census.service"
  helper_src="$source_root/scripts/operator/process-census.py"
  doc_src="$source_root/docs/operator-fork.md"

  local f
  for f in "$timer_src" "$service_src" "$helper_src" "$doc_src"; do
    [[ -f "$f" ]] || die "missing source file: $f"
  done

  git -C "$source_root" merge-base --is-ancestor "$SOURCE_COMMIT" HEAD ||
    die "source commit $SOURCE_COMMIT is not an ancestor of HEAD"

  local status_out
  status_out=$(git -C "$source_root" status --porcelain)
  [[ -z "$status_out" ]] || die "checkout is not clean"

  local fork_url fork_normalized
  fork_url=$(git -C "$source_root" remote get-url fork 2>/dev/null) ||
    die "remote 'fork' is not configured"
  # Allow only the conventional optional trailing .git
  fork_normalized=${fork_url%.git}
  [[ "$fork_normalized" == "$EXPECTED_FORK_BASE" ]] ||
    die "remote fork must be ${EXPECTED_FORK_BASE}[.git]; got: $fork_url"

  assert_sha "$timer_src" "$EXPECTED_TIMER_SHA"
  assert_sha "$service_src" "$EXPECTED_SERVICE_SHA"
  assert_sha "$helper_src" "$EXPECTED_HELPER_SHA"
  assert_sha "$doc_src" "$EXPECTED_DOC_SHA"

  # Non-interactive sudo only (never prompt).
  sudo -n true 2>/dev/null || die "sudo -n not available (passwordless sudo required)"

  # ---- backup (before replacement) -------------------------------------------

  local ts backup_dir dest
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  backup_dir="${BACKUP_PARENT}/process-census-reboot-trigger-${SOURCE_COMMIT:0:9}-${ts}"
  mkdir -p -- "$backup_dir"

  for dest in "$DEST_HELPER" "$DEST_SERVICE" "$DEST_TIMER" "$DEST_DOC"; do
    if [[ -e "$dest" ]]; then
      cp -a -- "$dest" "$backup_dir/$(basename -- "$dest").before"
    fi
  done

  printf 'backup=%s\n' "$backup_dir"

  # ---- root-owned staging (closes source-to-sudo TOCTOU) ---------------------

  STAGING_DIR=
  trap 'cleanup_staging' EXIT

  STAGING_DIR=$(
    sudo -n mktemp -d -p "$STAGING_PARENT" "${STAGING_NAME_PREFIX}XXXXXX"
  ) || die "failed to create root-owned staging directory"
  is_valid_staging_dir "$STAGING_DIR" ||
    die "staging path failed validation: ${STAGING_DIR:-empty}"

  # Copy reviewed inputs under root authority into staging (root reads sources
  # at this moment). Modes match final destinations for staged verify.
  sudo -n install -o root -g root -m 0644 -- "$timer_src" \
    "$STAGING_DIR/$STAGED_TIMER_NAME"
  sudo -n install -o root -g root -m 0644 -- "$service_src" \
    "$STAGING_DIR/$STAGED_SERVICE_NAME"
  sudo -n install -o root -g root -m 0755 -- "$helper_src" \
    "$STAGING_DIR/$STAGED_HELPER_NAME"
  sudo -n install -o root -g root -m 0644 -- "$doc_src" \
    "$STAGING_DIR/$STAGED_DOC_NAME"

  # Any mutation of sources between preflight hash and this point fails here.
  assert_sha "$STAGING_DIR/$STAGED_TIMER_NAME" "$EXPECTED_TIMER_SHA"
  assert_sha "$STAGING_DIR/$STAGED_SERVICE_NAME" "$EXPECTED_SERVICE_SHA"
  assert_sha "$STAGING_DIR/$STAGED_HELPER_NAME" "$EXPECTED_HELPER_SHA"
  assert_sha "$STAGING_DIR/$STAGED_DOC_NAME" "$EXPECTED_DOC_SHA"
  assert_owner_mode "$STAGING_DIR/$STAGED_TIMER_NAME" 'root:root:644'
  assert_owner_mode "$STAGING_DIR/$STAGED_SERVICE_NAME" 'root:root:644'
  assert_owner_mode "$STAGING_DIR/$STAGED_HELPER_NAME" 'root:root:755'
  assert_owner_mode "$STAGING_DIR/$STAGED_DOC_NAME" 'root:root:644'

  # ---- install destinations only from staged files ---------------------------

  sudo -n install -d -o root -g root -m 0755 -- "$(dirname -- "$DEST_HELPER")"
  sudo -n install -o root -g root -m 0755 -- \
    "$STAGING_DIR/$STAGED_HELPER_NAME" "$DEST_HELPER"
  sudo -n install -o root -g root -m 0644 -- \
    "$STAGING_DIR/$STAGED_SERVICE_NAME" "$DEST_SERVICE"
  sudo -n install -o root -g root -m 0644 -- \
    "$STAGING_DIR/$STAGED_TIMER_NAME" "$DEST_TIMER"
  sudo -n install -d -o root -g root -m 0755 -- "$DEST_DOC_DIR"
  sudo -n install -o root -g root -m 0644 -- \
    "$STAGING_DIR/$STAGED_DOC_NAME" "$DEST_DOC"

  # Verify installed destinations before any systemd reload/restart.
  assert_sha "$DEST_TIMER" "$EXPECTED_TIMER_SHA"
  assert_sha "$DEST_SERVICE" "$EXPECTED_SERVICE_SHA"
  assert_sha "$DEST_HELPER" "$EXPECTED_HELPER_SHA"
  assert_sha "$DEST_DOC" "$EXPECTED_DOC_SHA"
  assert_owner_mode "$DEST_HELPER" 'root:root:755'
  assert_owner_mode "$DEST_SERVICE" 'root:root:644'
  assert_owner_mode "$DEST_TIMER" 'root:root:644'
  assert_owner_mode "$DEST_DOC" 'root:root:644'

  # PCT-002: identity of any pre-existing snapshot (do not count it later).
  local baseline_identity boot_id
  baseline_identity=$(census_file_identity "$CENSUS_OUT")
  boot_id=$(tr -d '[:space:]' </proc/sys/kernel/random/boot_id)
  [[ -n "$boot_id" ]] || die "cannot read current boot_id"

  # Load new units; enable timer; restart *only* the census timer so the changed
  # active timer unit is loaded. Never touch Paseo or any other service.
  sudo -n systemctl daemon-reload
  sudo -n systemctl enable -- "$TIMER_UNIT"
  sudo -n systemctl restart -- "$TIMER_UNIT"

  local enabled_state active_state next_rt next_mono next_trigger
  enabled_state=$(systemctl is-enabled -- "$TIMER_UNIT" 2>/dev/null || true)
  [[ "$enabled_state" == enabled ]] || die "timer not enabled (got: $enabled_state)"

  active_state=$(systemctl is-active -- "$TIMER_UNIT" 2>/dev/null || true)
  [[ "$active_state" == active ]] || die "timer not active (got: $active_state)"

  next_rt=$(systemctl show "$TIMER_UNIT" -p NextElapseUSecRealtime --value 2>/dev/null || true)
  next_mono=$(systemctl show "$TIMER_UNIT" -p NextElapseUSecMonotonic --value 2>/dev/null || true)
  next_trigger=''
  if [[ -n "$next_rt" && "$next_rt" != infinity && "$next_rt" != n/a ]]; then
    next_trigger="realtime:$next_rt"
  elif [[ -n "$next_mono" && "$next_mono" != infinity && "$next_mono" != n/a ]]; then
    next_trigger="monotonic:$next_mono"
  else
    die "timer has no finite next trigger (rt=${next_rt:-empty} mono=${next_mono:-empty})"
  fi

  # Bounded poll: ≤5s sleep, total wall < 60s. Two distinct complete same-boot
  # empty-error captures with identities strictly subsequent to baseline.
  local deadline first_identity second_identity poll_sleep candidate step remaining
  local first_at second_at
  deadline=$((SECONDS + 55))
  first_identity=''
  second_identity=''
  first_at=''
  second_at=''
  poll_sleep=2

  while ((SECONDS < deadline)); do
    candidate=$(valid_census_capture_identity "$CENSUS_OUT" "$boot_id")
    step=$(accept_capture_step "$baseline_identity" "$first_identity" "$candidate")
    case "$step" in
      first:*)
        first_identity=${step#first:}
        first_at=${first_identity%%$'\t'*}
        ;;
      second:*)
        second_identity=${step#second:}
        second_at=${second_identity%%$'\t'*}
        break
        ;;
      ignore) ;;
      *)
        die "internal error: unexpected accept_capture_step result: $step"
        ;;
    esac
    remaining=$((deadline - SECONDS))
    ((remaining <= 0)) && break
    if ((remaining < poll_sleep)); then
      sleep "$remaining"
    else
      sleep "$poll_sleep"
    fi
  done

  [[ -n "$first_identity" ]] ||
    die "no complete same-boot empty-errors census capture subsequent to baseline within poll window"
  [[ -n "$second_identity" ]] ||
    die "only one distinct subsequent complete capture within poll window (first=$first_at)"

  # Staging cleanup via EXIT trap (also runs on failure paths after stage create).
  cleanup_staging
  trap - EXIT

  # Compact proof
  printf 'PASS process-census-reboot-trigger\n'
  printf 'source_commit=%s\n' "$SOURCE_COMMIT"
  printf 'backup=%s\n' "$backup_dir"
  printf 'installed helper=%s service=%s timer=%s doc=%s\n' \
    "$EXPECTED_HELPER_SHA" "$EXPECTED_SERVICE_SHA" "$EXPECTED_TIMER_SHA" "$EXPECTED_DOC_SHA"
  printf 'modes helper=root:root:755 units+doc=root:root:644\n'
  printf 'timer enabled=%s active=%s next=%s\n' "$enabled_state" "$active_state" "$next_trigger"
  printf 'captures boot_id=%s first=%s second=%s\n' "$boot_id" "$first_at" "$second_at"
  printf 'baseline_identity_present=%s\n' \
    "$([[ -n "$baseline_identity" ]] && printf yes || printf no)"
  jq -c '{captured_at, boot_id, complete, errors: (.errors|length), processes: (.processes|length)}' \
    "$CENSUS_OUT"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  process_census_install_main "$@"
fi
