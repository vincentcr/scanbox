#!/bin/bash
# Base packages: HPLIP + SANE + the bits AirSane needs to build.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# cloud-init runs its own apt on first boot; racing it fails with a lock error.
echo "waiting for any in-progress apt to finish..."
for _ in $(seq 1 120); do
  fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1 || break
  sleep 5
done

apt-get update -qq
apt-get install -y --no-install-recommends \
  hplip libsane-hpaio sane-utils \
  avahi-daemon avahi-utils libavahi-client-dev \
  build-essential cmake git pkg-config \
  libsane-dev libjpeg-dev libpng-dev libusb-1.0-0-dev \
  imagemagick poppler-utils ca-certificates curl

# hpaio is what actually speaks to the M276nw; make sure SANE loads it.
grep -qx hpaio /etc/sane.d/dll.conf || echo hpaio >> /etc/sane.d/dll.conf

# HPLIP is fussy about this; wrong perms here silently break scanning.
install -d -m 755 -o root -g root /var/lib/hp

echo "10-packages: done"
