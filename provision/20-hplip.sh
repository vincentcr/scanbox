#!/bin/bash
# Legacy HP acquisition runtime. This is installed only after routing selects
# hpaio; WSD jobs never execute this script.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "waiting for any in-progress apt to finish..."
for _ in $(seq 1 120); do
  fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1 || break
  sleep 5
done

apt-get update -qq
apt-get install -y --no-install-recommends \
  hplip libsane-hpaio imagemagick poppler-utils curl

# hpaio is what actually speaks to the scanner; make sure SANE loads it.
grep -qx hpaio /etc/sane.d/dll.conf || echo hpaio >> /etc/sane.d/dll.conf

# HPLIP expects this state directory to be root-owned and world-readable.
install -d -m 755 -o root -g root /var/lib/hp

echo "20-hplip: done"
