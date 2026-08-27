#!/bin/bash
# Detect where the fed sheet physically ends, and crop to it.
#
# The M276nw's feeder always scans its full 381mm (15") travel regardless of the
# sheet, and exposes no page-length sensor through SANE. But past the sheet's
# trailing edge it images its own backing, which comes back as a perfectly constant
# grey (254 -> 65278 at 16-bit) while real paper is 255 or textured. So the physical
# edge is directly observable: it is the row where that constant run begins.
#
# This measures the SHEET, not the ink -- a blank legal page is still legal.
#
#   autofit.sh measure <image> [dpi]  -> "<name> <height_px> <detected_in>"
#   autofit.sh crop    <image> <px>
set -euo pipefail

cmd="$1"; img="$2"

case "$cmd" in
measure)
  res="${3:-300}"
  h=$(identify -format "%h" "$img")

  # Collapse a central column to one pixel wide so each value is a row mean. The
  # central 80% dodges the shadow the feeder casts along the left/right margins.
  mapfile -t v < <(convert "$img" -gravity center -crop 80%x100%+0+0 +repage \
                     -colorspace Gray -depth 16 -resize 1x"${h}"! txt:- \
                   | awk -F'[()]' 'NR>1{print $2}')

  backing="${v[$((h-1))]}"          # bottom of the bed is always past the sheet
  # Paper white (65535) sits just 257 above the backing (65278) -- one 8-bit level
  # -- so the threshold has to stay well under that or the sheet reads as backing.
  tol=128
  # ...but a single noisy row must not end the walk, so require a sustained run.
  run_needed=5

  edge=$h; run=0
  for (( i=h-1; i>=0; i-- )); do
    d=$(( ${v[$i]} - backing )); (( d < 0 )) && d=$(( -d ))
    if (( d > tol )); then
      run=$(( run + 1 ))
      if (( run >= run_needed )); then edge=$(( i + run )); break; fi
    else
      run=0
    fi
  done

  # No backing run worth the name: the sheet is at least as long as the bed.
  if (( h - edge < res / 4 )); then
    echo "full ${h} $(awk -v h="$h" -v r="$res" 'BEGIN{printf "%.2f", h/r}')"
    exit 0
  fi

  detected_in=$(awk -v e="$edge" -v r="$res" 'BEGIN{printf "%.2f", e/r}')

  # Snap to a standard size only when the measurement is already within ~4mm of one,
  # so output is a clean letter/legal/A4 page. Otherwise report the measured length
  # rather than inventing a size the sheet does not have.
  snap=$(( res * 15 / 100 ))
  for spec in "a5:8.27" "letter:11" "a4:11.69" "legal:14"; do
    px=$(awk -v i="${spec##*:}" -v r="$res" 'BEGIN{printf "%d", i*r}')
    d=$(( edge - px )); (( d < 0 )) && d=$(( -d ))
    if (( d <= snap )); then
      echo "${spec%%:*} ${px} ${detected_in}"; exit 0
    fi
  done
  echo "custom ${edge} ${detected_in}"
  ;;
crop)
  px="$3"
  w=$(identify -format "%w" "$img")
  h=$(identify -format "%h" "$img")
  (( px >= h )) && exit 0
  convert "$img" -crop "${w}x${px}+0+0" +repage "$img"
  ;;
*) echo "usage: autofit.sh measure|crop ..." >&2; exit 2 ;;
esac
