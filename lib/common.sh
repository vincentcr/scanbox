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
# The message lives in a file, not a variable, because the thing that knows how a
# long step is going is often not the shell drawing the spinner: the scan status
# arrives on a pipeline, which bash runs in a subshell, and a variable set there
# would never reach the animation. A file is visible to both.
SPINNER_MSG_FILE=""

spinner_start() {
  local msg="$1"
  spinner_stop
  SPINNER_MSG_FILE="$(mktemp -t scanbox-spin)"
  printf '%s' "$msg" > "$SPINNER_MSG_FILE"
  # Not a terminal (piped, CI): print one plain line instead of animating.
  if [ ! -t 2 ]; then printf '%s...\n' "$msg" >&2; return 0; fi
  printf '\033[?25l' >&2                       # hide cursor
  # $$ stays the main shell's pid even inside a subshell, which is exactly the
  # anchor we need: spinners are sometimes started from within $( ), where
  # SPINNER_PID lands in that subshell and the top-level trap cannot reach it.
  # Watching the main shell means the spinner cleans itself up regardless.
  local anchor=$$ msgfile="$SPINNER_MSG_FILE"
  (
    # Background jobs of a non-interactive shell inherit SIGINT ignored, so
    # without this Ctrl-C leaves the spinner drawing over the user's prompt.
    trap 'exit 0' INT TERM HUP
    frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
    i=0 last=""
    while kill -0 "$anchor" 2>/dev/null; do
      i=$(( (i + 1) % ${#frames[@]} ))
      cur="$(cat "$msgfile" 2>/dev/null)"
      # A shortening message must not leave the tail of the previous one behind.
      [ "$cur" != "$last" ] && printf '\r\033[K' >&2
      last="$cur"
      printf '\r  %s %s ' "${frames[$i]}" "$cur" >&2
      sleep 0.1
    done
  ) >/dev/null &
  SPINNER_PID=$!
}

# Change what a running spinner says. Safe when no spinner is running, and safe
# from inside a subshell -- which is the whole point of the file.
spinner_update() {
  [ -n "$SPINNER_MSG_FILE" ] || return 0
  printf '%s' "$1" > "$SPINNER_MSG_FILE" 2>/dev/null || true
  return 0
}

# Print a line without the spinner scribbling over it. The animation redraws
# itself on its next tick.
spinner_say() {
  [ -t 2 ] && printf '\r\033[K' >&2
  printf 'scanbox: %s\n' "$*" >&2
  return 0
}

# Clear the line and restore the cursor. Safe to call when nothing is running.
spinner_stop() {
  # Clearing the line is only right if something was drawn on it, but restoring
  # the cursor must be unconditional: a spinner started inside $( ) hid the
  # cursor from a subshell we can no longer see, and leaving a terminal with no
  # cursor is a nasty thing to do to someone.
  if [ -n "$SPINNER_PID" ]; then
    kill "$SPINNER_PID" 2>/dev/null || true
    wait "$SPINNER_PID" 2>/dev/null || true
    SPINNER_PID=""
    [ -t 2 ] && printf '\r\033[K' >&2
  fi
  if [ -n "$SPINNER_MSG_FILE" ]; then
    rm -f "$SPINNER_MSG_FILE"
    SPINNER_MSG_FILE=""
  fi
  [ -t 2 ] && printf '\033[?25h' >&2
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
