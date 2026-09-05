#!/bin/bash
# Acquisition-only WSD scan inside the guest. The host supplies an explicit
# sane-airscan device environment, so this never depends on guest multicast.
#
# guest-wsd-scan.sh <SANE_AIRSCAN_DEVICE=...> <device> <source|auto>
#   <auto-feeder-or-empty> <mode> <dpi> <page> <run-id> <guest-output-dir>
set -euo pipefail

AIRSCAN_ENV="$1"
DEVICE="$2"
SOURCE="$3"
AUTO_FEEDER="$4"
MODE="$5"
DPI="$6"
PAGE="$7"
RUNID="$8"
OUTDIR="$9"

case "$AIRSCAN_ENV" in SANE_AIRSCAN_DEVICE=wsd:*) ;; *) exit 2 ;; esac
case "$RUNID" in *[!0-9-]*|'') exit 2 ;; esac
case "$OUTDIR" in /tmp/scanbox-wsd-*) ;; *) exit 2 ;; esac
export "$AIRSCAN_ENV"

PGID_FILE="/tmp/scanbox-run-$RUNID.pgid"
echo $$ > "$PGID_FILE"
cleanup() { rm -f "$PGID_FILE"; }
trap cleanup EXIT

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

height=""
case "$PAGE" in
  letter) height=279.4 ;;
  legal)  height=355.6 ;;
  a4)     height=297 ;;
  auto|max) ;;
  *) echo "unsupported page size: $PAGE" >&2; exit 2 ;;
esac

page_count() {
  local count=0 page
  for page in "$OUTDIR"/p*.png; do
    [ -e "$page" ] && count=$((count + 1))
  done
  echo "$count"
}

progress_filter() {
  local line
  while IFS= read -r -d $'\r' line; do
    case "$line" in
      *Progress:*) printf 'PROGRESS %s\n' "${line##*Progress: }" ;;
    esac
  done
  return 0
}

scan_errors() {
  tr '\r' '\n' < "$1" 2>/dev/null |
    grep -vE '^(Progress:|[[:space:]]*)$' || true
}

run_scanimage() {
  local error_file="$1"; shift
  local status
  set +e
  "$@" --progress 2>&1 >/dev/null | tee "$error_file" | progress_filter
  status=${PIPESTATUS[0]}
  set -e
  return "$status"
}

scan_to_file() {
  local error_file="$1" source="$2" target="$3"
  if [ -n "$height" ]; then
    run_scanimage "$error_file" \
      scanimage -d "$DEVICE" --source "$source" --mode "$MODE" \
      --resolution "$DPI" -y "$height" --format=png -o "$target"
  else
    run_scanimage "$error_file" \
      scanimage -d "$DEVICE" --source "$source" --mode "$MODE" \
      --resolution "$DPI" --format=png -o "$target"
  fi
}

scan_feeder() {
  local source="$1"
  local index=1 target status
  while :; do
    target=$(printf '%s/p%04d.png' "$OUTDIR" "$index")
    rm -f "$target"
    echo "PHASE scanning feeder page $index"
    set +e
    scan_to_file "$OUTDIR/feeder.err" "$source" "$target"
    status=$?
    set -e

    if [ -s "$target" ]; then
      # Some WSD devices, including the validated Xerox, return NO_DOCS from
      # the final sane_read after delivering a complete page. scanimage's
      # batch mode discards that page; -o leaves it intact for us to keep.
      if grep -qiE 'out of documents|no documents' "$OUTDIR/feeder.err"; then
        return 0
      fi
      [ "$status" -eq 0 ] || return 1
      index=$((index + 1))
      continue
    fi

    rm -f "$target"
    if grep -qiE 'out of documents|no documents' "$OUTDIR/feeder.err"; then
      return 0
    fi
    return 1
  done
}

scan_flatbed() {
  scan_to_file "$OUTDIR/flatbed.err" Flatbed "$OUTDIR/p0001.png" || {
      scan_errors "$OUTDIR/flatbed.err" >&2
      exit 1
    }
}

case "$SOURCE" in
  Flatbed)
    scan_flatbed
    used=Flatbed
    ;;
  ADF|'ADF Duplex')
    feeder_failed=0
    scan_feeder "$SOURCE" || feeder_failed=1
    used="$SOURCE"
    ;;
  auto)
    if [ -n "$AUTO_FEEDER" ]; then
      feeder_failed=0
      scan_feeder "$AUTO_FEEDER" || feeder_failed=1
      if [ "$(page_count)" -eq 0 ]; then
        if [ "$feeder_failed" -eq 0 ]; then
          echo "PHASE feeder empty; scanning flatbed"
          scan_flatbed
          used=Flatbed
        else
          scan_errors "$OUTDIR/feeder.err" >&2
          echo "feeder probe failed; refusing to fall back after a scan may have begun" >&2
          exit 1
        fi
      else
        used="$AUTO_FEEDER"
      fi
    else
      scan_flatbed
      used=Flatbed
    fi
    ;;
  *) echo "unsupported source: $SOURCE" >&2; exit 2 ;;
esac

count=$(page_count)
if [ "$count" -eq 0 ]; then
  scan_errors "$OUTDIR/feeder.err" >&2
  echo "no pages were scanned" >&2
  exit 1
fi

if [ "$used" != "Flatbed" ] && [ "$feeder_failed" -ne 0 ]; then
  echo "TRUNCATED $count"
  echo "WARNING feeder stopped before reporting it was empty after $count page(s)"
fi

echo "SOURCE $used"
echo "PAGES $count"
for page in "$OUTDIR"/p*.png; do
  echo "PAGE $page"
done
