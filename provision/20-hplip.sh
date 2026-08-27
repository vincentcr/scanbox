#!/bin/bash
# Point HPLIP at the printer, install the binary scan plugin, prove it answers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/printer.env"

URI="hpaio:/net/${PRINTER_MODEL}?ip=${PRINTER_IP}"
HPLIP_VERSION="$(awk -F= '/^version=/{print $2; exit}' /etc/hp/hplip.conf)"

echo "== reachability =="
if ! timeout 5 bash -c "exec 3<>/dev/tcp/${PRINTER_IP}/8289" 2>/dev/null; then
  echo "FATAL: ${PRINTER_IP}:8289 (HP SOAP scan) unreachable from the VM" >&2
  exit 1
fi
echo "  ${PRINTER_IP}:8289 reachable"

# The M276nw scans over HP's SOAPHT protocol (models.dat: scan-type=5), which is
# implemented in the closed-source plugin bb_soapht.so -- NOT in the hplip package.
# Note models.dat says plugin=0 for this model; that flag only covers printing, so
# it is misleading here. Without the plugin, scanimage fails with a bare
# "Error during device I/O" and the real reason only shows up in the journal.
if [[ -e /usr/share/hplip/scan/plugins/bb_soapht.so ]]; then
  echo "== plugin already installed =="
else
  echo "== installing HPLIP ${HPLIP_VERSION} binary plugin =="
  RUN="hplip-${HPLIP_VERSION}-plugin.run"
  # HP's own CDN returns 403; OpenPrinting mirrors the same file.
  SRC=/tmp/hpplugin-src
  install -d "${SRC}"
  curl -fsSL -o "${SRC}/${RUN}" \
    "https://www.openprinting.org/download/printdriver/auxfiles/HP/plugins/${RUN}"
  # `yes` accepts the HP EULA prompt; there is no non-interactive flag.
  yes | hp-plugin -i -p "${SRC}"
fi

[[ -e /usr/share/hplip/scan/plugins/bb_soapht.so ]] || {
  echo "FATAL: bb_soapht.so still missing after plugin install" >&2; exit 1; }

install -d -m 755 /etc/scanbox
printf 'SCANBOX_DEVICE=%s\n' "${URI}" > /etc/scanbox/device.env
echo "  device: ${URI}"

echo "== scanner probe =="
if scanimage -d "${URI}" --dont-scan 2>&1; then
  echo "20-hplip: done"
else
  echo "probe failed; check: journalctl | grep scanimage" >&2
  exit 1
fi
