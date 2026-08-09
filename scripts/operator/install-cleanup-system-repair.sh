#!/usr/bin/env bash
# Attended self-verifying installer for the complete cleanup-system repair.
# Fail-closed: requires unprivileged user, noninteractive sudo, clean exact
# fork/main HEAD, and hard-pinned payload SHA-256s before any mutation.
# Sequence: preflight → child timer installer once → permanent user backup →
# atomic user-owned probe/policy install. Never restarts Paseo/SHAB/census service.
# Never mutates schedule state.
set -euo pipefail

readonly EXPECTED_FORK_BASE='https://github.com/suislurper/paseo'
readonly EXPECTED_USER=user
readonly EXPECTED_HOME=/home/user
readonly EXPECTED_PASEO_DIR=/home/user/.paseo
readonly EXPECTED_BIN_DIR=/home/user/.paseo/bin
readonly BACKUP_PARENT=/mnt/data/paseo-runtime/artifacts/operator-install/backups

# Hard-pinned reviewed payload digests (exact bytes from clean fork head).
readonly EXPECTED_CHILD_INSTALLER_SHA=d0ac1cfb95a8639e09188174ad55cd8c22d4416311fc7efb17954bae376a827e
readonly EXPECTED_TIMER_SHA=c6ae90d72818c12fff69ce6e120f21bfb25cea564d3bedbd314fe5588b2dc6d0
readonly EXPECTED_SERVICE_SHA=0eb3a9e3bb537cb2ede2cede371784e3c7882ccf623e2c35baa4cf37cc5919d6
readonly EXPECTED_HELPER_SHA=6241954df045e75cbd669d3136718b83395fb9a71f218bd58884da56a33daf92
readonly EXPECTED_DOC_SHA=e88df8a832fd4d514b96885c8ee607744751aed9c9fb6bde5453d23da1a57a7c
readonly EXPECTED_WORKTREE_PROBE_SHA=d2173ef4183e62d906b42a06f9ea3cff02f0081bcbcf9255fba764c66f281aa7
readonly EXPECTED_AGENT_SCRATCH_SHA=814facdbabce625f8c905fdb148cd2d29686f159037aad947806efa7ec22910d
readonly EXPECTED_LEGACY_TMP_SHA=7dbb2efec2cb95675680591bf0d58a762ed350e6f0fd720db5e18d45215ebe45
readonly EXPECTED_WORKTREE_POLICY_SHA=aa7a2891c0ff30401991ef6510b7fd754020c35e76afd14d27e54f4999cafe09
readonly EXPECTED_SCRATCH_POLICY_SHA=a5b16e918dbf33336b7139ad07b648a10d1f0f42d958cb8c4397156e93b1e51d
readonly EXPECTED_WAKE_TXT_SHA=93580f3e80d671ce6f8eac974927eab1cccb5b50af198bdf141d88f0d259b48c

# Production destinations (user-owned allowlist).
readonly DEST_WORKTREE_PROBE=/home/user/.paseo/bin/worktree_cleanup_probe.py
readonly DEST_AGENT_SCRATCH=/home/user/.paseo/bin/agent-scratch-cleanup.py
readonly DEST_LEGACY_TMP=/home/user/.paseo/bin/legacy-tmp-quarantine.py
readonly DEST_WORKTREE_POLICY=/home/user/.paseo/WORKTREE_CLEANUP_POLICY.md
readonly DEST_SCRATCH_POLICY=/home/user/.paseo/AGENT_SCRATCH_CLEANUP_POLICY.md
readonly DEST_WAKE_TXT=/home/user/.paseo/worktree-cleanup-wake.txt

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

# True when every existing path component of absolute $1 is not a symlink.
# Also rejects relative paths and ".." components.
path_has_no_symlink_components() {
  local path=$1
  local cur part
  [[ -n "$path" && "$path" == /* ]] || return 1
  [[ "$path" != *'/..'* && "$path" != *'/../'* ]] || return 1
  cur=
  local IFS='/'
  # shellcheck disable=SC2086
  set -- $path
  for part in "$@"; do
    [[ -n "$part" ]] || continue
    cur="${cur}/${part}"
    if [[ -L "$cur" ]]; then
      return 1
    fi
    if [[ -e "$cur" && ! -d "$cur" && "$cur" != "$path" ]]; then
      return 1
    fi
  done
  return 0
}

# Require absolute path exists as a real directory with no symlink components
# and that readlink -f agrees with the path string (no alternate resolution).
assert_real_dir() {
  local path=$1
  [[ -d "$path" ]] || die "not a directory: $path"
  path_has_no_symlink_components "$path" || die "symlink component in path: $path"
  local resolved
  resolved=$(readlink -f -- "$path")
  [[ "$resolved" == "$path" ]] || die "path does not resolve to itself (symlink?): $path -> $resolved"
}

# Reject when $1 exists as a symlink, or any of its parent components is a symlink.
assert_dest_not_symlink() {
  local path=$1
  local parent
  parent=$(dirname -- "$path")
  assert_real_dir "$parent"
  if [[ -L "$path" ]]; then
    die "destination is a symlink (refusing): $path"
  fi
}

# fsync a regular file, then fsync its parent directory (durability for rename).
fsync_file_and_parent() {
  local path=$1
  python3 - "$path" <<'PY'
import os
import sys

path = sys.argv[1]
fd = os.open(path, os.O_RDONLY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
parent = os.path.dirname(path) or "."
dirfd = os.open(parent, os.O_RDONLY)
try:
    os.fsync(dirfd)
finally:
    os.close(dirfd)
PY
}

# Pre-install inventory row for one target: sha/mode/owner/size or absent.
preinstall_inventory_json() {
  local path=$1
  python3 - "$path" <<'PY'
import json
import os
import stat
import sys
import hashlib
import pwd
import grp

path = sys.argv[1]
if not os.path.lexists(path):
    print(json.dumps({"path": path, "present": False}, separators=(",", ":")))
    raise SystemExit(0)
if os.path.islink(path):
    print(json.dumps({
        "path": path,
        "present": True,
        "is_symlink": True,
        "link_target": os.readlink(path),
    }, separators=(",", ":")))
    raise SystemExit(0)
st = os.lstat(path)
mode = stat.S_IMODE(st.st_mode)
h = hashlib.sha256()
with open(path, "rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
owner = pwd.getpwuid(st.st_uid).pw_name
group = grp.getgrgid(st.st_gid).gr_name
print(json.dumps({
    "path": path,
    "present": True,
    "is_symlink": False,
    "sha256": h.hexdigest(),
    "mode": f"{mode:04o}",
    "owner": f"{owner}:{group}",
    "size": st.st_size,
}, separators=(",", ":")))
PY
}

# Atomically install reviewed src → dest with mode; owner must end as expect_user:expect_user.
# Same-directory temp + fsync + rename + parent fsync. Cleans exact temp on failure.
# Args: src dest mode expected_sha [expect_user]
atomic_install_user_file() {
  local src=$1
  local dest=$2
  local mode=$3
  local expected_sha=$4
  local expect_user=${5:-$EXPECTED_USER}
  local dest_dir base tmp="" actual owner_mode

  # die() exits the process; always drop the exact temp first.
  _atomic_die() {
    local t=${tmp:-}
    tmp=
    if [[ -n "$t" && -e "$t" ]]; then
      rm -f -- "$t" || true
    fi
    die "$@"
  }

  [[ -f "$src" ]] || die "missing source for atomic install: $src"
  assert_sha "$src" "$expected_sha"
  dest_dir=$(dirname -- "$dest")
  base=$(basename -- "$dest")
  assert_dest_not_symlink "$dest"

  tmp=$(mktemp -p "$dest_dir" ".${base}.tmp.XXXXXX") || die "mktemp failed for $dest"

  cp -- "$src" "$tmp" || _atomic_die "copy to temp failed for $dest"
  chmod -- "$mode" "$tmp" || _atomic_die "chmod temp failed for $dest"
  owner_mode=$(stat -c '%U:%G:%a' -- "$tmp")
  [[ "$owner_mode" == "${expect_user}:${expect_user}:${mode}" ]] ||
    _atomic_die "temp owner/mode mismatch for $dest: got $owner_mode want ${expect_user}:${expect_user}:${mode}"

  fsync_file_and_parent "$tmp" || _atomic_die "fsync temp failed for $dest"
  if ! mv -f -- "$tmp" "$dest"; then
    _atomic_die "atomic rename failed for $dest"
  fi
  # Successful rename: temp path is now dest; do not delete.
  tmp=
  fsync_file_and_parent "$dest" || die "fsync dest failed for $dest"

  if [[ -L "$dest" ]]; then
    die "destination became a symlink after install: $dest"
  fi
  actual=$(file_sha256 "$dest")
  [[ "$actual" == "$expected_sha" ]] || die "post-install sha mismatch for $dest: got $actual want $expected_sha"
  owner_mode=$(stat -c '%U:%G:%a' -- "$dest")
  [[ "$owner_mode" == "${expect_user}:${expect_user}:${mode}" ]] ||
    die "post-install owner/mode mismatch for $dest: got $owner_mode want ${expect_user}:${expect_user}:${mode}"
}

# Create permanent backup dir under backup_parent; fail if it already exists.
# Args: head backup_parent dest1 [dest2 ...]
# Copies each existing destination with metadata; writes inventory.json.
# Prints the backup directory path.
create_user_payload_backup() {
  local head=$1
  local backup_parent=$2
  shift 2
  local -a dests=("$@")
  local ts backup_dir dest name inv_tmp row first backup_name

  ((${#dests[@]} > 0)) || die "create_user_payload_backup requires at least one dest"
  [[ -n "$head" ]] || die "create_user_payload_backup requires head"
  [[ -n "$backup_parent" && "$backup_parent" == /* ]] || die "backup_parent must be absolute"

  ts=$(date -u +%Y%m%dT%H%M%SZ)
  backup_dir="${backup_parent}/cleanup-system-repair-${head}-${ts}"
  [[ ! -e "$backup_dir" ]] || die "backup directory already exists (refusing overwrite): $backup_dir"
  mkdir -p -- "$backup_parent"
  assert_real_dir "$backup_parent"
  mkdir -- "$backup_dir" || die "failed to create exclusive backup dir: $backup_dir"

  inv_tmp=$(mktemp -p "$backup_dir" ".inventory.json.tmp.XXXXXX")
  {
    printf '[\n'
    first=1
    for dest in "${dests[@]}"; do
      row=$(preinstall_inventory_json "$dest")
      if [[ -e "$dest" || -L "$dest" ]]; then
        name=$(basename -- "$dest")
        backup_name="${name}.before"
        if [[ -L "$dest" ]]; then
          cp -a -- "$dest" "$backup_dir/$backup_name" || die "backup copy failed: $dest"
        elif [[ -f "$dest" ]]; then
          cp -a -- "$dest" "$backup_dir/$backup_name" || die "backup copy failed: $dest"
          fsync_file_and_parent "$backup_dir/$backup_name"
        else
          die "refusing to backup non-regular destination: $dest"
        fi
      fi
      if [[ $first -eq 1 ]]; then
        first=0
      else
        printf ',\n'
      fi
      printf '  %s' "$(
        python3 -c 'import json,sys; o=json.loads(sys.argv[1]); o["installed_sha256"]=None; print(json.dumps(o,separators=(",",":")))' "$row"
      )"
    done
    printf '\n]\n'
  } >"$inv_tmp"
  fsync_file_and_parent "$inv_tmp"
  mv -f -- "$inv_tmp" "$backup_dir/inventory.json"
  fsync_file_and_parent "$backup_dir/inventory.json"
  fsync_file_and_parent "$backup_dir"
  printf '%s\n' "$backup_dir"
}

# Rewrite inventory installed_sha256 fields after successful installs.
# Args: inventory_path dest1 sha1 dest2 sha2 ...
finalize_inventory_installed_shas() {
  local inventory_path=$1
  shift
  python3 - "$inventory_path" "$@" <<'PY'
import json
import os
import sys

path = sys.argv[1]
args = sys.argv[2:]
if len(args) % 2 != 0:
    raise SystemExit("finalize_inventory_installed_shas: dest/sha pairs required")
sha_by_path = dict(zip(args[0::2], args[1::2]))
with open(path, encoding="utf-8") as f:
    rows = json.load(f)
for row in rows:
    p = row.get("path")
    if p in sha_by_path:
        row["installed_sha256"] = sha_by_path[p]
with open(path, "w", encoding="utf-8") as f:
    json.dump(rows, f, separators=(",", ":"), indent=2)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
PY
  fsync_file_and_parent "$inventory_path"
}

# Preflight gates used by main (and tests). Does not mutate targets.
# Args: source_root
# Uses ambient: id, HOME, sudo, git.
# Prints: head=<sha> on success.
preflight_identity_and_git() {
  local source_root=$1

  [[ "$(id -u)" != "0" ]] || die "must not run as root (EUID=0); run unprivileged after sudo -v"
  [[ "$(id -un)" == "$EXPECTED_USER" ]] || die "install user must be ${EXPECTED_USER}; got $(id -un)"
  [[ "${HOME:-}" == "$EXPECTED_HOME" ]] || die "HOME must be ${EXPECTED_HOME}; got ${HOME:-empty}"

  local passwd_home
  passwd_home=$(getent passwd "$EXPECTED_USER" | cut -d: -f6)
  [[ "$passwd_home" == "$EXPECTED_HOME" ]] ||
    die "passwd home for ${EXPECTED_USER} is not ${EXPECTED_HOME}"

  assert_real_dir "$EXPECTED_HOME"
  assert_real_dir "$EXPECTED_PASEO_DIR"

  # Noninteractive sudo before backups/staging/child/target changes.
  sudo -n true 2>/dev/null || die "sudo -n not available (passwordless/cached sudo required; run sudo -v first)"

  [[ -d "$source_root/.git" || -f "$source_root/.git" ]] ||
    die "cannot derive git checkout (root=$source_root)"
  git -C "$source_root" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
    die "not a git worktree: $source_root"
  local bare
  bare=$(git -C "$source_root" rev-parse --is-bare-repository 2>/dev/null || printf 'true')
  [[ "$bare" == "false" ]] || die "refusing bare repository: $source_root"

  local status_out
  status_out=$(git -C "$source_root" status --porcelain)
  [[ -z "$status_out" ]] || die "checkout is not clean (tracked+untracked must be empty)"

  local fork_url fork_normalized
  fork_url=$(git -C "$source_root" remote get-url fork 2>/dev/null) ||
    die "remote 'fork' is not configured"
  fork_normalized=${fork_url%.git}
  [[ "$fork_normalized" == "$EXPECTED_FORK_BASE" ]] ||
    die "remote fork must be ${EXPECTED_FORK_BASE}[.git]; got: $fork_url"

  local head remote_head
  head=$(git -C "$source_root" rev-parse HEAD)
  [[ "$head" =~ ^[0-9a-f]{40}$ ]] || die "unexpected HEAD format: $head"
  remote_head=$(git -C "$source_root" ls-remote fork refs/heads/main | awk 'NR==1 {print $1}')
  [[ -n "$remote_head" ]] || die "git ls-remote fork refs/heads/main returned empty"
  [[ "$remote_head" =~ ^[0-9a-f]{40}$ ]] || die "unexpected remote HEAD format: $remote_head"
  [[ "$head" == "$remote_head" ]] ||
    die "local HEAD ($head) != fork/main ($remote_head); install only exact reviewed clean fork head"

  printf 'head=%s\n' "$head"
}

# Verify all hard-pinned source payloads under source_root / script_dir.
# Args: source_root script_dir
preflight_payload_pins() {
  local source_root=$1
  local script_dir=$2
  local child_installer timer_src service_src helper_src doc_src
  local worktree_probe_src agent_scratch_src legacy_tmp_src
  local worktree_policy_src scratch_policy_src wake_src
  local f

  child_installer="$script_dir/install-process-census-reboot-trigger.sh"
  timer_src="$source_root/scripts/operator/systemd/paseo-process-census.timer"
  service_src="$source_root/scripts/operator/systemd/paseo-process-census.service"
  helper_src="$source_root/scripts/operator/process-census.py"
  doc_src="$source_root/docs/operator-fork.md"
  worktree_probe_src="$source_root/scripts/operator/worktree-cleanup-probe.py"
  agent_scratch_src="$source_root/scripts/operator/agent-scratch-cleanup.py"
  legacy_tmp_src="$source_root/scripts/operator/legacy-tmp-quarantine.py"
  worktree_policy_src="$source_root/scripts/operator/policy/WORKTREE_CLEANUP_POLICY.md"
  scratch_policy_src="$source_root/scripts/operator/policy/AGENT_SCRATCH_CLEANUP_POLICY.md"
  wake_src="$source_root/scripts/operator/policy/worktree-cleanup-wake.txt"

  for f in \
    "$child_installer" \
    "$timer_src" \
    "$service_src" \
    "$helper_src" \
    "$doc_src" \
    "$worktree_probe_src" \
    "$agent_scratch_src" \
    "$legacy_tmp_src" \
    "$worktree_policy_src" \
    "$scratch_policy_src" \
    "$wake_src"; do
    [[ -f "$f" ]] || die "missing source file: $f"
    [[ ! -L "$f" ]] || die "source must not be a symlink: $f"
  done

  assert_sha "$child_installer" "$EXPECTED_CHILD_INSTALLER_SHA"
  assert_sha "$timer_src" "$EXPECTED_TIMER_SHA"
  assert_sha "$service_src" "$EXPECTED_SERVICE_SHA"
  assert_sha "$helper_src" "$EXPECTED_HELPER_SHA"
  assert_sha "$doc_src" "$EXPECTED_DOC_SHA"
  assert_sha "$worktree_probe_src" "$EXPECTED_WORKTREE_PROBE_SHA"
  assert_sha "$agent_scratch_src" "$EXPECTED_AGENT_SCRATCH_SHA"
  assert_sha "$legacy_tmp_src" "$EXPECTED_LEGACY_TMP_SHA"
  assert_sha "$worktree_policy_src" "$EXPECTED_WORKTREE_POLICY_SHA"
  assert_sha "$scratch_policy_src" "$EXPECTED_SCRATCH_POLICY_SHA"
  assert_sha "$wake_src" "$EXPECTED_WAKE_TXT_SHA"
}

# ---- main (skipped when sourced for unit tests) ------------------------------

cleanup_system_repair_main() {
  require_cmd git
  require_cmd sha256sum
  require_cmd sudo
  require_cmd date
  require_cmd awk
  require_cmd mkdir
  require_cmd cp
  require_cmd mv
  require_cmd stat
  require_cmd mktemp
  require_cmd dirname
  require_cmd basename
  require_cmd readlink
  require_cmd rm
  require_cmd python3
  require_cmd id
  require_cmd chmod
  require_cmd getent
  require_cmd cut

  local script_path script_dir source_root head_line head
  script_path=$(readlink -f -- "${BASH_SOURCE[0]}")
  script_dir=$(dirname -- "$script_path")
  source_root=$(cd -- "$script_dir/../.." && pwd -P)

  # Preflight only (no backup/stage/target mutation yet). Bin may already exist;
  # if present, require it to be a real non-symlink directory before proceeding.
  if [[ -e "$EXPECTED_BIN_DIR" ]]; then
    assert_real_dir "$EXPECTED_BIN_DIR"
  else
    path_has_no_symlink_components "$EXPECTED_BIN_DIR" || die "symlink in bin path components"
  fi

  head_line=$(preflight_identity_and_git "$source_root")
  head=${head_line#head=}
  [[ "$head" =~ ^[0-9a-f]{40}$ ]] || die "preflight did not yield head: $head_line"

  preflight_payload_pins "$source_root" "$script_dir"

  # Child timer installer exactly once (root units + activation proof).
  local child_installer child_out child_rc
  child_installer="$script_dir/install-process-census-reboot-trigger.sh"
  set +e
  child_out=$("$child_installer" 2>&1)
  child_rc=$?
  set -e
  printf '%s\n' "$child_out"
  [[ $child_rc -eq 0 ]] || die "child installer failed with exit $child_rc"
  printf '%s\n' "$child_out" | grep -q '^PASS process-census-reboot-trigger$' ||
    die "child installer did not emit expected PASS line"

  # User-owned target mutations only after child PASS.
  if [[ ! -e "$EXPECTED_BIN_DIR" ]]; then
    mkdir -m 0755 -- "$EXPECTED_BIN_DIR" || die "failed to create ${EXPECTED_BIN_DIR}"
  fi
  assert_real_dir "$EXPECTED_BIN_DIR"

  # Permanent user-payload backup (after child PASS only).
  local backup_dir
  backup_dir=$(
    create_user_payload_backup "$head" "$BACKUP_PARENT" \
      "$DEST_WORKTREE_PROBE" \
      "$DEST_AGENT_SCRATCH" \
      "$DEST_LEGACY_TMP" \
      "$DEST_WORKTREE_POLICY" \
      "$DEST_SCRATCH_POLICY" \
      "$DEST_WAKE_TXT"
  )
  printf 'user_backup=%s\n' "$backup_dir"

  # Atomic install of user-owned reviewed payloads.
  local worktree_probe_src agent_scratch_src legacy_tmp_src
  local worktree_policy_src scratch_policy_src wake_src
  worktree_probe_src="$source_root/scripts/operator/worktree-cleanup-probe.py"
  agent_scratch_src="$source_root/scripts/operator/agent-scratch-cleanup.py"
  legacy_tmp_src="$source_root/scripts/operator/legacy-tmp-quarantine.py"
  worktree_policy_src="$source_root/scripts/operator/policy/WORKTREE_CLEANUP_POLICY.md"
  scratch_policy_src="$source_root/scripts/operator/policy/AGENT_SCRATCH_CLEANUP_POLICY.md"
  wake_src="$source_root/scripts/operator/policy/worktree-cleanup-wake.txt"

  atomic_install_user_file "$worktree_probe_src" "$DEST_WORKTREE_PROBE" 755 "$EXPECTED_WORKTREE_PROBE_SHA"
  atomic_install_user_file "$agent_scratch_src" "$DEST_AGENT_SCRATCH" 755 "$EXPECTED_AGENT_SCRATCH_SHA"
  atomic_install_user_file "$legacy_tmp_src" "$DEST_LEGACY_TMP" 755 "$EXPECTED_LEGACY_TMP_SHA"
  atomic_install_user_file "$worktree_policy_src" "$DEST_WORKTREE_POLICY" 644 "$EXPECTED_WORKTREE_POLICY_SHA"
  atomic_install_user_file "$scratch_policy_src" "$DEST_SCRATCH_POLICY" 644 "$EXPECTED_SCRATCH_POLICY_SHA"
  atomic_install_user_file "$wake_src" "$DEST_WAKE_TXT" 644 "$EXPECTED_WAKE_TXT_SHA"

  finalize_inventory_installed_shas "$backup_dir/inventory.json" \
    "$DEST_WORKTREE_PROBE" "$EXPECTED_WORKTREE_PROBE_SHA" \
    "$DEST_AGENT_SCRATCH" "$EXPECTED_AGENT_SCRATCH_SHA" \
    "$DEST_LEGACY_TMP" "$EXPECTED_LEGACY_TMP_SHA" \
    "$DEST_WORKTREE_POLICY" "$EXPECTED_WORKTREE_POLICY_SHA" \
    "$DEST_SCRATCH_POLICY" "$EXPECTED_SCRATCH_POLICY_SHA" \
    "$DEST_WAKE_TXT" "$EXPECTED_WAKE_TXT_SHA"

  # Deterministic PASS summary.
  printf 'PASS cleanup-system-repair\n'
  printf 'head=%s\n' "$head"
  printf 'backup=%s\n' "$backup_dir"
  printf 'child=PASS process-census-reboot-trigger\n'
  printf 'installed worktree_probe=%s mode=755\n' "$EXPECTED_WORKTREE_PROBE_SHA"
  printf 'installed agent_scratch=%s mode=755\n' "$EXPECTED_AGENT_SCRATCH_SHA"
  printf 'installed legacy_tmp=%s mode=755\n' "$EXPECTED_LEGACY_TMP_SHA"
  printf 'installed worktree_policy=%s mode=644\n' "$EXPECTED_WORKTREE_POLICY_SHA"
  printf 'installed scratch_policy=%s mode=644\n' "$EXPECTED_SCRATCH_POLICY_SHA"
  printf 'installed wake_txt=%s mode=644\n' "$EXPECTED_WAKE_TXT_SHA"
  printf 'note=Paseo/SHAB services were not restarted; schedule state was not mutated\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  cleanup_system_repair_main "$@"
fi
