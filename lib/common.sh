#!/usr/bin/env bash
# Shared host-side helpers.
#
# These run on macOS, which ships bash 3.2 and none of flock/setsid/timeout. Keep
# everything here portable to that: no mapfile, no associative arrays, no ${x,,}.

STATE_DIR="${SCANBOX_STATE_DIR:-$HOME/.local/state/scanbox}"
CONFIG_FILE="${SCANBOX_CONFIG:-$HOME/.config/scanbox/config}"

say()  { printf '%s\n' "$*" >&2; }
warn() { printf 'scanner: %s\n' "$*" >&2; }
die()  { printf 'scanner: %s\n' "$*" >&2; exit 1; }

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

lock_release() { rm -rf "$LOCK_DIR"; }

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
