#!/bin/bash
# Runs INSIDE the VM. Scans, sizes the pages, assembles a PDF, and prints a small
# machine-readable summary the host parses.
#
#   guest-scan.sh <uri> <auto|ADF|Flatbed> <mode> <dpi> <page> <lossless 0|1> <name>
#
# Output lines: "PAGE <n> <size> <measured_in>", "SOURCE <x>", "PAGES <n>", "OUT <path>"
set -euo pipefail

URI="$1"; SOURCE="$2"; MODE="$3"; DPI="$4"; PAGE="$5"; LOSSLESS="$6"; NAME="$7"

AUTOFIT=/usr/local/lib/scanbox/autofit.sh
OUTDIR=/tmp/scanbox-out
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
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

page_count() { ls "$tmp"/p*.png 2>/dev/null | wc -l | tr -d ' '; }

scan_adf() {
  # --batch keeps pulling sheets until the feeder reports empty. An empty feeder
  # fails in ~0.3s with "Document feeder out of documents" and moves no paper,
  # which is what makes source autodetection cheap.
  scanimage -d "$URI" --source ADF --mode "$MODE" --resolution "$DPI" \
    $adf_height $compress --format=png --batch="$tmp/p%04d.png" \
    >/dev/null 2>"$tmp/adf.err" || true
}

scan_flatbed() {
  scanimage -d "$URI" --source Flatbed --mode "$MODE" --resolution "$DPI" \
    $height $compress --format=png -o "$tmp/p0001.png" \
    >/dev/null 2>"$tmp/bed.err" \
    || { sed 's/^/  /' "$tmp/bed.err" >&2; exit 1; }
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
  sed 's/^/  /' "$tmp/adf.err" >&2 2>/dev/null || true
  echo "no pages were scanned" >&2
  exit 1
fi

# Size each sheet from its own trailing edge. ADF only -- the flatbed has no
# backing to measure against, since the lid is the same white as paper.
if [ "$used" = "ADF" ] && [ "$PAGE" = "auto" ]; then
  for f in "$tmp"/p*.png; do
    set -- $(bash "$AUTOFIT" measure "$f" "$DPI")
    bash "$AUTOFIT" crop "$f" "$2"
    echo "PAGE $(basename "$f" .png) $1 $3"
  done
fi

# Every page is kept, blanks included -- predictable beats clever.
convert "$tmp"/p*.png -quality 88 "$OUTDIR/$NAME.pdf"

echo "SOURCE $used"
echo "PAGES $n"
echo "OUT $OUTDIR/$NAME.pdf"
