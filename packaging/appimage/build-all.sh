#!/bin/bash
# Build both AppImages with one command.
#
# The backends share the venv, download cache, PyInstaller work directory, and
# AppDir, so they are built sequentially. Running the two scripts in parallel
# would race over those paths.
#
# Output:
#   dist/Blurb-nvidia-x86_64.AppImage
#   dist/Blurb-rocm-x86_64.AppImage
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

if [ $# -gt 0 ]; then
    echo "usage: $0" >&2
    exit 2
fi

echo "==> Building NVIDIA AppImage"
"$ROOT/packaging/appimage/build-nvidia.sh"

echo
echo "==> Building ROCm AppImage"
"$ROOT/packaging/appimage/build-rocm.sh"

echo
echo "Both AppImages are ready:"
ls -lh \
    "$ROOT/dist/Blurb-nvidia-x86_64.AppImage" \
    "$ROOT/dist/Blurb-rocm-x86_64.AppImage"
