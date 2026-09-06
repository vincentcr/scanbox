#!/bin/bash
# WSD backend. The shared SANE frontend is provisioned separately by 10-core;
# this script adds only sane-airscan and never installs or initializes HPLIP.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "waiting for any in-progress apt to finish..."
for _ in $(seq 1 120); do
  fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1 || break
  sleep 5
done

apt-get update -qq
apt-get install -y --no-install-recommends sane-airscan

# Debian enables packaged backends through dll.d, but accepting either layout
# keeps this working if the base image changes its SANE configuration.
if [ -f /etc/sane.d/dll.conf ] && [ ! -d /etc/sane.d/dll.d ]; then
  grep -qx airscan /etc/sane.d/dll.conf || echo airscan >> /etc/sane.d/dll.conf
fi

echo "40-airscan: done"
