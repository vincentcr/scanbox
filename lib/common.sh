#!/usr/bin/env bash
# Shared host-side helpers.
#
# These run on macOS, which ships bash 3.2 and none of flock/setsid/timeout. Keep
# everything here portable to that: no mapfile, no associative arrays, no ${x,,}.

STATE_DIR="${SCANBOX_STATE_DIR:-$HOME/.local/state/scanbox}"
CONFIG_FILE="${SCANBOX_CONFIG:-$HOME/.config/scanbox/config}"

say()  { printf '%s\n' "$*" >&2; }
warn() { printf 'scanbox: %s\n' "$*" >&2; }
die()  { printf 'scanbox: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Progress spinner.
#
# Several steps here are slow with nothing to show for it -- resolving a printer
# that is not on this network burns 6s, and hp-makeuri can sit for 30 -- and
# silence is indistinguishable from a hang.
#
# The spinner writes to stderr only, so stdout stays the machine-readable result.
# Its subshell's stdout MUST go to /dev/null: several callers run inside $(...),
# and a background job holding that pipe open would keep the command substitution
# waiting forever -- causing exactly the hang this is meant to dispel.
# ---------------------------------------------------------------------------

SPINNER_PID=""

spinner_start() {
  local msg="$1"
  spinner_stop
  # Not a terminal (piped, CI): print one plain line instead of animating.
  if [ ! -t 2 ]; then printf '%s...\n' "$msg" >&2; return 0; fi
  printf '\033[?25l' >&2                       # hide cursor
  (
    frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
    i=0
    while :; do
      i=$(( (i + 1) % ${#frames[@]} ))
      printf '\r  %s %s ' "${frames[$i]}" "$msg" >&2
      sleep 0.1
    done
  ) >/dev/null 2>&2 &
  SPINNER_PID=$!
}

# Clear the line and restore the cursor. Safe to call when nothing is running.
spinner_stop() {
  # Only emit the clear/show-cursor sequence if something was actually spinning,
  # so the EXIT trap does not spray escape codes on every ordinary exit.
  if [ -n "$SPINNER_PID" ]; then
    kill "$SPINNER_PID" 2>/dev/null || true
    wait "$SPINNER_PID" 2>/dev/null || true
    SPINNER_PID=""
    [ -t 2 ] && printf '\r\033[K\033[?25h' >&2
  fi
  return 0
}

# Report how a spun step ended, on its own line.
spinner_done() { spinner_stop; [ -n "${1:-}" ] && say "$1"; return 0; }

# macOS has no `timeout`, so perl provides it. Note perl must *stay* as the parent
# and reap the child rather than exec'ing into it: an exec'd process killed by
# SIGALRM makes the calling shell print "Alarm clock: 14" job-control noise, and
# loses the exit status besides.

# Propagates the command's exit status; 124 if it had to be killed. Use where
# failure actually matters.
bounded() {
  local secs="$1"; shift
  perl -e '
    my $secs = shift @ARGV;
    my $pid = fork();
    if ($pid == 0) { exec(@ARGV); exit 127; }   # exit, or a failed exec falls through
    local $SIG{ALRM} = sub { kill "TERM", $pid };
    alarm $secs; waitpid($pid, 0); alarm 0;
    exit(($? & 127) ? 124 : ($? >> 8));
  ' "$secs" "$@"
}

# For commands that stream until we stop them (dns-sd browses). Hitting the timeout
# is the expected way these end, so it is success, not failure.
bounded_stream() {
  local secs="$1"; shift
  perl -e '
    my $secs = shift @ARGV;
    my $pid = open(my $fh, "-|", @ARGV) or exit 0;
    eval {
      local $SIG{ALRM} = sub { die "timeout\n" };
      alarm $secs;
      print while <$fh>;
      alarm 0;
    };
    kill "TERM", $pid; close $fh;
    exit 0;
  ' "$secs" "$@" 2>/dev/null
}

# macOS has no `flock`. mkdir is atomic on every filesystem we care about, so it is
# the portable mutex. The pid inside lets us detect a holder that died mid-scan.
LOCK_DIR="$STATE_DIR/lock"

lock_acquire() {
  local waited=0 limit="${1:-600}"
  mkdir -p "$STATE_DIR"
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    local holder
    holder=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
    if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
      warn "clearing stale lock from dead process $holder"
      rm -rf "$LOCK_DIR"
      continue
    fi
    [ "$waited" -eq 0 ] && say "another scan is in progress; waiting..."
    sleep 2
    waited=$((waited + 2))
    [ "$waited" -ge "$limit" ] && die "timed out waiting for the lock held by ${holder:-unknown}"
  done
  echo $$ > "$LOCK_DIR/pid"
}

# Only ever release a lock we actually hold. A blanket rm would let a cleanup trap
# in one process delete the lock another process is scanning under.
lock_release() {
  [ -d "$LOCK_DIR" ] || return 0
  local owner
  owner=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  [ "$owner" = "$$" ] || return 0
  rm -rf "$LOCK_DIR"
}

lock_held() { [ -d "$LOCK_DIR" ]; }

# Seconds since epoch that the last scan finished. BSD stat.
last_used_file() { echo "$STATE_DIR/last-used"; }

mark_used() { mkdir -p "$STATE_DIR"; date +%s > "$(last_used_file)"; }

seconds_since_use() {
  local f
  f="$(last_used_file)"
  [ -f "$f" ] || { echo 999999; return; }
  local then now
  then=$(cat "$f" 2>/dev/null || echo 0)
  now=$(date +%s)
  echo $((now - then))
}
