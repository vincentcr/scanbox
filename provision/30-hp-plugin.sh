#!/bin/bash
# Install HP's closed-source scan plugin. This script is reachable only through
# the HPLIP capability and remains idempotent across repeated provisioning.
set -euo pipefail

plugin_present() {
  for f in /usr/share/hplip/scan/plugins/bb_*.so; do [ -e "$f" ] && return 0; done
  return 1
}

if plugin_present; then
  echo "30-hp-plugin: already installed"
  exit 0
fi

HPLIP_VERSION="$(awk -F= '/^version=/{print $2; exit}' /etc/hp/hplip.conf)"
RUN="hplip-${HPLIP_VERSION}-plugin.run"
SRC=/tmp/hpplugin-src
install -d "$SRC"

echo "30-hp-plugin: fetching ${RUN}"
# HP's CDN returns 403; OpenPrinting mirrors the identical file.
curl -fsSL -o "${SRC}/${RUN}" \
  "https://www.openprinting.org/download/printdriver/auxfiles/HP/plugins/${RUN}"

# hp-plugin has no non-interactive flag. `yes` receives SIGPIPE when hp-plugin
# exits, so the installed payload—not the pipeline status—is the success signal.
LOG=/tmp/hp-plugin-install.log
yes | hp-plugin -i -p "$SRC" >"$LOG" 2>&1 || true

if ! plugin_present; then
  echo "FATAL: no bb_*.so scan backend after plugin install" >&2
  tail -30 "$LOG" >&2
  exit 1
fi

echo "30-hp-plugin: done"
