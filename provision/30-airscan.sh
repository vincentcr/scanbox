#!/bin/bash
# WSD acquisition runtime. This path intentionally does not install HPLIP or
# HP's proprietary plugin: sane-airscan speaks WSD itself.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# cloud-init may still own apt's locks immediately after a fresh VM is created.
echo "waiting for any in-progress apt to finish..."
for _ in $(seq 1 120); do
  fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1 || break
  sleep 5
done

apt-get update -qq
apt-get install -y --no-install-recommends sane-airscan sane-utils ca-certificates

# Debian enables packaged backends through dll.d, but accepting either layout
# keeps this working if the image changes to a SANE build using dll.conf.
if [ -f /etc/sane.d/dll.conf ] && [ ! -d /etc/sane.d/dll.d ]; then
  grep -qx airscan /etc/sane.d/dll.conf || echo airscan >> /etc/sane.d/dll.conf
fi

echo "30-airscan: done"
