#!/bin/bash
# Republish the SANE scanner over eSCL/AirScan so macOS sees it natively
# (Preview, Image Capture) without any client-side software.
set -euo pipefail

SRC=/opt/AirSane

if [[ -d "${SRC}/.git" ]]; then
  git -C "${SRC}" pull --ff-only
else
  git clone --depth 1 https://github.com/SimulPiscator/AirSane.git "${SRC}"
fi

cmake -S "${SRC}" -B "${SRC}/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "${SRC}/build" -j"$(nproc)"
cmake --install "${SRC}/build"

systemctl daemon-reload
systemctl enable --now airsaned.service
sleep 2
systemctl is-active --quiet airsaned.service && echo "30-airsane: airsaned active" || {
  journalctl -u airsaned.service -n 40 --no-pager >&2
  exit 1
}
