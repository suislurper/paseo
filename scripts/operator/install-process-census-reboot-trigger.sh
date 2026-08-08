#!/usr/bin/env bash
# Non-interactive operator installer for the process-census OnActiveSec timer fix.
# Fail-closed: verifies source SHA/ancestry/fork remote before any privilege use.
# Restarts only paseo-process-census.timer — never Paseo or other services.
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

# ---- preflight (no privilege) ------------------------------------------------

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

script_path=$(readlink -f -- "${BASH_SOURCE[0]}")
script_dir=$(dirname -- "$script_path")
# scripts/operator/ -> repo root
source_root=$(cd -- "$script_dir/../.." && pwd -P)

[[ -d "$source_root/.git" || -f "$source_root/.git" ]] ||
  die "cannot derive git checkout from $script_path (root=$source_root)"

timer_src="$source_root/scripts/operator/systemd/paseo-process-census.timer"
service_src="$source_root/scripts/operator/systemd/paseo-process-census.service"
helper_src="$source_root/scripts/operator/process-census.py"
doc_src="$source_root/docs/operator-fork.md"

for f in "$timer_src" "$service_src" "$helper_src" "$doc_src"; do
  [[ -f "$f" ]] || die "missing source file: $f"
done

git -C "$source_root" merge-base --is-ancestor "$SOURCE_COMMIT" HEAD ||
  die "source commit $SOURCE_COMMIT is not an ancestor of HEAD"

status_out=$(git -C "$source_root" status --porcelain)
[[ -z "$status_out" ]] || die "checkout is not clean"

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

# ---- backup (before replacement) ---------------------------------------------

ts=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="${BACKUP_PARENT}/process-census-reboot-trigger-${SOURCE_COMMIT:0:9}-${ts}"
mkdir -p -- "$backup_dir"

for dest in "$DEST_HELPER" "$DEST_SERVICE" "$DEST_TIMER" "$DEST_DOC"; do
  if [[ -e "$dest" ]]; then
    cp -a -- "$dest" "$backup_dir/$(basename -- "$dest").before"
  fi
done

printf 'backup=%s\n' "$backup_dir"

# ---- install (sudo -n only) --------------------------------------------------

sudo -n install -d -o root -g root -m 0755 -- "$(dirname -- "$DEST_HELPER")"
sudo -n install -o root -g root -m 0755 -- "$helper_src" "$DEST_HELPER"
sudo -n install -o root -g root -m 0644 -- "$service_src" "$DEST_SERVICE"
sudo -n install -o root -g root -m 0644 -- "$timer_src" "$DEST_TIMER"
sudo -n install -d -o root -g root -m 0755 -- "$DEST_DOC_DIR"
sudo -n install -o root -g root -m 0644 -- "$doc_src" "$DEST_DOC"

# Load new units; enable timer; restart *only* the census timer so the changed
# active timer unit is loaded. Never touch Paseo or any other service.
sudo -n systemctl daemon-reload
sudo -n systemctl enable -- "$TIMER_UNIT"
sudo -n systemctl restart -- "$TIMER_UNIT"

# ---- post-install verification -----------------------------------------------

assert_sha "$DEST_TIMER" "$EXPECTED_TIMER_SHA"
assert_sha "$DEST_SERVICE" "$EXPECTED_SERVICE_SHA"
assert_sha "$DEST_HELPER" "$EXPECTED_HELPER_SHA"
assert_sha "$DEST_DOC" "$EXPECTED_DOC_SHA"
assert_owner_mode "$DEST_HELPER" 'root:root:755'
assert_owner_mode "$DEST_SERVICE" 'root:root:644'
assert_owner_mode "$DEST_TIMER" 'root:root:644'
assert_owner_mode "$DEST_DOC" 'root:root:644'

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

boot_id=$(tr -d '[:space:]' </proc/sys/kernel/random/boot_id)
[[ -n "$boot_id" ]] || die "cannot read current boot_id"

# Bounded poll: ≤5s sleep, total wall < 60s. Collect two distinct complete
# same-boot captures with empty errors (timer fires ~10s after activate).
deadline=$((SECONDS + 55))
first_at=''
second_at=''
poll_sleep=2

while ((SECONDS < deadline)); do
  if [[ -f "$CENSUS_OUT" ]]; then
    # shellcheck disable=SC2016
    if parsed=$(
      jq -er \
        --arg boot "$boot_id" \
        '
        select(
          .complete == true and
          (.errors | type == "array") and
          (.errors | length) == 0 and
          .boot_id == $boot and
          (.captured_at | type == "string") and
          (.captured_at | length) > 0
        ) | .captured_at
        ' \
        "$CENSUS_OUT" 2>/dev/null
    ); then
      if [[ -z "$first_at" ]]; then
        first_at=$parsed
      elif [[ "$parsed" != "$first_at" ]]; then
        second_at=$parsed
        break
      fi
    fi
  fi
  remaining=$((deadline - SECONDS))
  ((remaining <= 0)) && break
  if ((remaining < poll_sleep)); then
    sleep "$remaining"
  else
    sleep "$poll_sleep"
  fi
done

[[ -n "$first_at" ]] ||
  die "no complete same-boot empty-errors census capture within poll window"
[[ -n "$second_at" ]] ||
  die "only one distinct complete capture within poll window (first=$first_at)"

# Compact proof
printf 'PASS process-census-reboot-trigger\n'
printf 'source_commit=%s\n' "$SOURCE_COMMIT"
printf 'backup=%s\n' "$backup_dir"
printf 'installed helper=%s service=%s timer=%s doc=%s\n' \
  "$EXPECTED_HELPER_SHA" "$EXPECTED_SERVICE_SHA" "$EXPECTED_TIMER_SHA" "$EXPECTED_DOC_SHA"
printf 'modes helper=root:root:755 units+doc=root:root:644\n'
printf 'timer enabled=%s active=%s next=%s\n' "$enabled_state" "$active_state" "$next_trigger"
printf 'captures boot_id=%s first=%s second=%s\n' "$boot_id" "$first_at" "$second_at"
jq -c '{captured_at, boot_id, complete, errors: (.errors|length), processes: (.processes|length)}' \
  "$CENSUS_OUT"
