#!/bin/bash
# Runs INSIDE the VM. Scans, sizes the pages, assembles output, and prints a small
# machine-readable summary the host parses.
#
#   guest-scan.sh <uri> <auto|ADF|Flatbed> <mode> <dpi> <page> <lossless 0|1> <name> [runid] [format] [image 0|1] [split 0|1]
#
# Output lines: "PAGE <n> <size> <measured_in>", "SOURCE <x>", "PAGES <n>", "OUT <path>" (one
# or more, in page order)
set -euo pipefail

URI="$1"; SOURCE="$2"; MODE="$3"; DPI="$4"; PAGE="$5"; LOSSLESS="$6"; NAME="$7"
RUNID="${8:-}"
FORMAT="${9:-pdf}"
IMAGE="${10:-0}"
SPLIT="${11:-0}"

AUTOFIT=/usr/local/lib/scanbox/autofit.sh
OUTDIR=/tmp/scanbox-out

# The host cannot signal us. `limactl shell` rides lima's shared SSH
# ControlMaster, which outlives the client that borrowed it, and the session gets
# no TTY -- so sshd never sends SIGHUP when the host goes away. A Ctrl-C on the
# host therefore leaves this script and its scanimage running, still holding the
# printer's single scan session, and every later scan then fails with a bare
# "sane_start: Error during device I/O" that points nowhere useful. The host has
# to kill us by hand, and needs somewhere to read the id from.
#
# sshd already put us in a process group of our own with this shell as leader, so
# $$ is also the group id: killing -$$ takes scanimage down with us.
PGID_FILE=""
[ -n "$RUNID" ] && PGID_FILE="/tmp/scanbox-run-$RUNID.pgid"

tmp=$(mktemp -d)
cleanup() {
  rm -rf "$tmp"
  [ -n "$PGID_FILE" ] && rm -f "$PGID_FILE"
  return 0
}
trap cleanup EXIT

[ -n "$PGID_FILE" ] && echo $$ > "$PGID_FILE"

rm -rf "$OUTDIR"; mkdir -p "$OUTDIR"

# The scanner JPEG-compresses in transit by default; that is the only place image
# quality is actually lost, and no output format recovers it.
compress=""
[ "$LOSSLESS" = "1" ] && compress="--compression None"

case "$PAGE" in
  letter) height="-y 279.4" ;;
  legal)  height="-y 355.6" ;;
  a4)     height="-y 297" ;;
  *)      height="" ;;
esac

# The feeder always runs its full 381mm travel and cannot report page length, so
# `auto` scans the whole bed and finds the real trailing edge afterwards.
adf_height="$height"
[ -z "$adf_height" ] && adf_height="-y 381"

# Count without `ls`: an unmatched glob makes ls exit 2, which under `set -o
# pipefail` propagates and kills the script via `set -e` -- silently, before any
# "nothing was scanned" message can be printed. Zero pages is a normal outcome
# here (an empty feeder), not an error.
page_count() {
  local c=0 f
  for f in "$tmp"/p*.png; do [ -e "$f" ] && c=$((c + 1)); done
  echo "$c"
}

# --progress makes scanimage report "Progress: xx.x%" on stderr, separated by
# carriage returns so it overwrites one line on a terminal. Nobody is watching
# that terminal, so re-emit each reading on stdout as a PROGRESS line and let the
# host draw it. A lossless 1200dpi page is ~430MB and takes about 13 minutes; with
# only a spinner to look at, that is indistinguishable from a hang, which is how
# these end up cancelled halfway through.
#
# `2>&1 >/dev/null` is order-sensitive: it points stderr at the pipe and only then
# sends stdout to /dev/null, so the progress text is what flows down it. The image
# itself never goes to stdout -- it is written via -o/--batch.
# Read the CR-delimited records with bash's own `read`, not awk.
#
# mawk buffers its *input* when RS is anything but a newline, so it sat on every
# percentage until the scan ended and then emitted them in one burst -- a spinner
# that said "scanning" for thirteen minutes and flashed "18.1%" once on its way
# out. fflush() cannot fix that: the records had not been read yet, let alone
# printed. gawk and `stdbuf` would each do, but neither is guaranteed present in
# the guest, and bash reads pipes a byte at a time by construction -- which is
# precisely the property needed here, at a volume where the syscalls are free.
progress_filter() {
  local line
  while IFS= read -r -d $'\r' line; do
    case "$line" in
      *Progress:*) printf 'PROGRESS %s\n' "${line##*Progress: }" ;;
    esac
  done
  return 0
}

# scanimage's stderr now carries progress noise as well as real diagnostics. Only
# the diagnostics are worth showing a human.
scan_errors() { tr '\r' '\n' < "$1" 2>/dev/null | grep -vE '^(Progress:|[[:space:]]*)$' || true; }

# Run one scanimage, streaming progress and keeping stderr for diagnosis.
# Returns scanimage's own exit status, not the pipeline's.
run_scanimage() {
  local errfile="$1"; shift
  local rc
  set +e
  "$@" --progress 2>&1 >/dev/null | tee "$errfile" | progress_filter
  rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}

# The printer allows exactly one scan session, and it does not free it the instant
# the client goes away: after an aborted scan it keeps refusing new sessions for
# roughly 45 seconds, reporting a bare "Error during device I/O". Retrying quietly
# through that window is the difference between "it works" and an error that reads
# like the scanner is broken.
DEVICE_BUSY_TRIES=6
DEVICE_BUSY_WAIT=15

busy_error() { grep -q "Error during device I/O" "$1" 2>/dev/null; }

scan_with_retry() {
  local errfile="$1"; shift
  local i=1
  while :; do
    run_scanimage "$errfile" "$@" && return 0
    busy_error "$errfile" || return 1
    [ "$i" -ge "$DEVICE_BUSY_TRIES" ] && return 1
    # Surfaced by the host, so a wait this long never looks like a stall.
    echo "NOTE the scanner is still busy with a previous scan; retrying in ${DEVICE_BUSY_WAIT}s ($i/$DEVICE_BUSY_TRIES)"
    sleep "$DEVICE_BUSY_WAIT"
    i=$((i + 1))
  done
}

scan_adf() {
  # --batch keeps pulling sheets until the feeder reports empty. An empty feeder
  # fails in ~0.3s with "Document feeder out of documents" and moves no paper,
  # which is what makes source autodetection cheap.
  #
  # No retry here: an empty feeder is a normal, expected failure, and under `auto`
  # this call is just a probe for whether paper is loaded. Waiting a minute on a
  # busy device before even trying the flatbed would be the wrong trade.
  run_scanimage "$tmp/adf.err" \
    scanimage -d "$URI" --source ADF --mode "$MODE" --resolution "$DPI" \
    $adf_height $compress --format=png --batch="$tmp/p%04d.png" || true
}

scan_flatbed() {
  scan_with_retry "$tmp/bed.err" \
    scanimage -d "$URI" --source Flatbed --mode "$MODE" --resolution "$DPI" \
    $height $compress --format=png -o "$tmp/p0001.png" \
    || { scan_errors "$tmp/bed.err" | sed 's/^/  /' >&2; exit 1; }
}

case "$SOURCE" in
  ADF)     scan_adf; used=ADF ;;
  Flatbed) scan_flatbed; used=Flatbed ;;
  auto)
    scan_adf
    if [ "$(page_count)" -eq 0 ]; then
      scan_flatbed; used=Flatbed
    else
      used=ADF
    fi
    ;;
  *) echo "bad source: $SOURCE" >&2; exit 2 ;;
esac

n=$(page_count)
if [ "$n" -eq 0 ]; then
  if grep -q "out of documents" "$tmp/adf.err" 2>/dev/null; then
    echo "the document feeder is empty -- load it, or use 'scanbox bed'" >&2
  else
    scan_errors "$tmp/adf.err" | sed 's/^/    /' >&2 || true
    echo "no pages were scanned" >&2
  fi
  exit 1
fi

# A feeder batch has exactly one healthy ending: the feeder reports it is out of
# documents. Anything else -- a jam, a mis-feed, an I/O error partway through --
# leaves a batch that stopped early. Without this check that is indistinguishable
# from success and quietly yields a short PDF, which is how you lose a page and
# never find out.
if [ "$used" = "ADF" ] && ! grep -q "out of documents" "$tmp/adf.err" 2>/dev/null; then
  echo "TRUNCATED $n"
  {
    echo "the feeder stopped before reporting it was empty, after $n page(s)."
    echo "scanbox kept what it got, but sheets may be missing. scanimage said:"
    scan_errors "$tmp/adf.err" | sed 's/^/    /' | tail -5
  } >&2
fi

# Size each sheet from its own trailing edge. ADF only -- the flatbed has no
# backing to measure against, since the lid is the same white as paper.
if [ "$used" = "ADF" ] && [ "$PAGE" = "auto" ]; then
  i=0
  for f in "$tmp"/p*.png; do
    i=$((i + 1))
    echo "PHASE measuring page $i of $n"
    set -- $(bash "$AUTOFIT" measure "$f" "$DPI")
    bash "$AUTOFIT" crop "$f" "$2"
    echo "PAGE $(basename "$f" .png) $1 $3"
  done
fi

# --image describes intent rather than a particular container. Resolve it only
# now, because source=auto does not tell us whether the feeder or flatbed won
# until after the feeder probe. A joined feeder batch needs the one image format
# here that supports multiple pages. Lineart stays PNG even when the transfer was
# compressed: JPEG is a poor final encoding for hard one-bit edges and text.
if [ "$FORMAT" = "auto" ] && [ "$IMAGE" = "1" ]; then
  if [ "$used" = "ADF" ] && [ "$SPLIT" = "0" ]; then
    FORMAT=tiff
  elif [ "$LOSSLESS" = "1" ] || [ "$MODE" = "Lineart" ]; then
    FORMAT=png
  else
    FORMAT=jpeg
  fi
fi

# Every page is kept, blanks included -- predictable beats clever.
#
# Assembly can be its own slow step, right after the scanner has already gone
# quiet: a 430MB lossless page takes ImageMagick a while to convert, and silence
# here reads as a stall just as easily as silence during the scan did -- hence a
# phase line per format. PNG is the exception and the whole point of offering it:
# the pages are already PNG, so there is nothing to convert, only to copy.
outs=()
case "$FORMAT" in
  pdf)
    if [ "$SPLIT" = "1" ]; then
      echo "PHASE building the PDF page$([ "$n" -eq 1 ] || echo s)"
      i=0
      for f in "$tmp"/p*.png; do
        i=$((i + 1))
        if [ "$n" -eq 1 ]; then dst="$OUTDIR/$NAME.pdf"
        else dst="$OUTDIR/$(printf '%s-p%03d.pdf' "$NAME" "$i")"
        fi
        convert "$f" -quality 88 "$dst"
        outs+=("$dst")
      done
    else
      echo "PHASE building the PDF"
      convert "$tmp"/p*.png -quality 88 "$OUTDIR/$NAME.pdf"
      outs=("$OUTDIR/$NAME.pdf")
    fi
    ;;
  tiff)
    if [ "$SPLIT" = "1" ]; then
      echo "PHASE building the TIFF page$([ "$n" -eq 1 ] || echo s)"
      i=0
      for f in "$tmp"/p*.png; do
        i=$((i + 1))
        if [ "$n" -eq 1 ]; then dst="$OUTDIR/$NAME.tiff"
        else dst="$OUTDIR/$(printf '%s-p%03d.tiff' "$NAME" "$i")"
        fi
        convert "$f" -compress Zip "$dst"
        outs+=("$dst")
      done
    else
      echo "PHASE building the TIFF"
      convert "$tmp"/p*.png -compress Zip "$OUTDIR/$NAME.tiff"
      outs=("$OUTDIR/$NAME.tiff")
    fi
    ;;
  png)
    echo "PHASE saving the PNG page$([ "$n" -eq 1 ] || echo s)"
    i=0
    for f in "$tmp"/p*.png; do
      i=$((i + 1))
      if [ "$n" -eq 1 ]; then dst="$OUTDIR/$NAME.png"
      else dst="$OUTDIR/$(printf '%s-p%03d.png' "$NAME" "$i")"
      fi
      cp "$f" "$dst"
      outs+=("$dst")
    done
    ;;
  jpeg)
    echo "PHASE building the JPEG$([ "$n" -eq 1 ] || echo s)"
    i=0
    for f in "$tmp"/p*.png; do
      i=$((i + 1))
      if [ "$n" -eq 1 ]; then dst="$OUTDIR/$NAME.jpg"
      else dst="$OUTDIR/$(printf '%s-p%03d.jpg' "$NAME" "$i")"
      fi
      convert "$f" -quality 92 "$dst"
      outs+=("$dst")
    done
    ;;
  *)
    echo "bad format: $FORMAT" >&2
    exit 2
    ;;
esac

echo "SOURCE $used"
echo "PAGES $n"
for o in "${outs[@]}"; do echo "OUT $o"; done
