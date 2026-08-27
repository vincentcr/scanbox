#!/bin/bash
# Install HP's closed-source scan plugin.
#
# Printer-agnostic on purpose: no address, no reachability check, no device URI.
# Which scanner we talk to is decided per-scan by the host, so first-run
# provisioning does not require the printer to be switched on -- and the same VM
# works for any HP scanner.
#
# Why this is needed at all: the M276nw scans over HP's SOAPHT protocol
# (models.dat scan-type=5), implemented in bb_soapht.so, which ships only in the
# proprietary plugin. models.dat reports plugin=0 for the model, but that flag
# covers printing only, so it is actively misleading here. Without the plugin,
# scanimage fails with a bare "Error during device I/O" and the real cause appears
# only in the journal.
set -euo pipefail

if [ -e /usr/share/hplip/scan/plugins/bb_soapht.so ]; then
  echo "20-plugin: already installed"
  exit 0
fi

HPLIP_VERSION="$(awk -F= '/^version=/{print $2; exit}' /etc/hp/hplip.conf)"
RUN="hplip-${HPLIP_VERSION}-plugin.run"
SRC=/tmp/hpplugin-src
install -d "$SRC"

echo "20-plugin: fetching ${RUN}"
# HP's own CDN returns 403; OpenPrinting mirrors the identical file.
curl -fsSL -o "${SRC}/${RUN}" \
  "https://www.openprinting.org/download/printdriver/auxfiles/HP/plugins/${RUN}"

# `yes` accepts the HP EULA prompt -- hp-plugin has no non-interactive flag. Its
# exit status is unusable: `yes` takes SIGPIPE when hp-plugin exits, so the pipeline
# reports 141 under `set -o pipefail` even on success. The installed file is the
# only trustworthy signal. Output goes to a log so the EULA does not spew over the
# terminal; it is shown only if something actually went wrong.
LOG=/tmp/hp-plugin-install.log
yes | hp-plugin -i -p "$SRC" >"$LOG" 2>&1 || true

if [ ! -e /usr/share/hplip/scan/plugins/bb_soapht.so ]; then
  echo "FATAL: bb_soapht.so missing after plugin install" >&2
  tail -30 "$LOG" >&2
  exit 1
fi

echo "20-plugin: done"
