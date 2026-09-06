#!/bin/bash
# Shared acquisition runtime. Keep this independent of every vendor backend so
# selecting WSD never pulls HPLIP or HP's proprietary plugin into the guest.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# cloud-init runs its own apt on first boot; racing it fails with a lock error.
echo "waiting for any in-progress apt to finish..."
for _ in $(seq 1 120); do
  fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1 || break
  sleep 5
done

apt-get update -qq
apt-get install -y --no-install-recommends sane-utils ca-certificates

echo "10-core: done"
