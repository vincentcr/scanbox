#!/bin/bash
# Base packages. Deliberately minimal: this VM exists only to run HPLIP's hpaio
# backend and turn the result into a PDF.
#
# cups arrives whether we want it or not -- hplip hard-depends on it -- but nothing
# here configures a print queue or enables the service.
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
  imagemagick poppler-utils \
  ca-certificates curl

# hpaio is what actually speaks to the scanner; make sure SANE loads it.
grep -qx hpaio /etc/sane.d/dll.conf || echo hpaio >> /etc/sane.d/dll.conf

# HPLIP is fussy about this; wrong perms here break scanning in a way that is hard
# to diagnose.
install -d -m 755 -o root -g root /var/lib/hp

echo "10-packages: done"
