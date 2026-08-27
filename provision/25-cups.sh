#!/bin/bash
# Register the printer as a CUPS queue.
#
# This is NOT about printing -- it is what makes `scanimage -L` enumerate the
# scanner. HPLIP discovers network devices by SLP/mDNS broadcast, which cannot
# cross the VM's NAT boundary, so discovery finds nothing. HPLIP also derives
# devices from configured hp: CUPS queues, and that path works over unicast.
# AirSane publishes whatever SANE enumerates, so without this it publishes nothing.
#
# We use lpadmin with a checked-in PPD rather than `hp-setup`, which insists on
# printing a physical test page.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/printer.env"

QUEUE="${PRINTER_MODEL}"
PPD="${ROOT}/provision/ppd/${PRINTER_MODEL}.ppd"

systemctl enable --now cups
# cupsd needs a moment before it will accept lpadmin
for _ in $(seq 1 30); do lpstat -r >/dev/null 2>&1 && break; sleep 1; done

if lpstat -v "${QUEUE}" >/dev/null 2>&1; then
  echo "== queue ${QUEUE} already present =="
else
  echo "== creating CUPS queue ${QUEUE} =="
  lpadmin -p "${QUEUE}" \
    -v "hp:/net/${PRINTER_MODEL}?ip=${PRINTER_IP}" \
    -P "${PPD}" -E
fi

echo "== SANE enumeration =="
timeout 90 scanimage -L 2>&1 | tee /tmp/scanimage-L.out
grep -q "hpaio:/net/${PRINTER_MODEL}" /tmp/scanimage-L.out || {
  echo "FATAL: scanner still not enumerated; AirSane will publish nothing" >&2
  exit 1
}
echo "25-cups: done"
