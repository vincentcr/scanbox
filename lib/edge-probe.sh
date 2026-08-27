#!/bin/bash
# Dump a row-brightness profile down a full-bed ADF scan, so we can see whether the
# sheet's trailing edge is physically visible (a shade change where paper ends and
# the ADF backing begins) rather than guessing page size from where the ink stops.
#
#   edge-probe.sh <image> [dpi] [from_inch] [to_inch]
set -euo pipefail
img="$1"; res="${2:-300}"; from="${3:-0}"; to="${4:-99}"
h=$(identify -format "%h" "$img")
w=$(identify -format "%w" "$img")
echo "image ${w}x${h} ($(awk -v h="$h" -v r="$res" 'BEGIN{printf "%.2f", h/r}')in)"
printf "  %-7s %-6s %s\n" inch row mean

# Squash a central column (dodging edge shadows) to one pixel wide, so each output
# row is that row's mean brightness. First parenthesised value is the 8-bit gray.
convert "$img" -gravity center -crop 60%x100%+0+0 +repage \
        -colorspace Gray -depth 8 -resize 1x"${h}"! txt:- \
  | awk -F'[()]' 'NR>1{print $2}' \
  | awk -v r="$res" -v f="$from" -v t="$to" '
      { v[NR-1]=$1 }
      END {
        step=int(r/8); if(step<1)step=1
        for(i=0;i<NR;i+=step){ inch=i/r
          if(inch>=f && inch<=t) printf "  %-7.2f %-6d %d\n", inch, i, v[i]
        }
      }'
