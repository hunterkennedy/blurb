#!/bin/bash
# Build the AMD/ROCm AppImage.
#
# Output: dist/Blurb-rocm-x86_64.AppImage
#
# The build itself does not need ROCm: PyInstaller first collects the normal
# CTranslate2 wheel, then this script swaps in the official ROCm wheel from
# CTranslate2's GitHub release. The *runtime* host needs ROCm 7.x installed
# (libamdhip64.so.7, libhipblas.so.3, libhiprand.so.1).
#
# This image will not start on a machine without the ROCm runtime, even for
# CPU use, because the ROCm CTranslate2 shared library links those libraries.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR="${BLURB_OUTPUT_DIR:-$ROOT/dist}"
mkdir -p "$OUT_DIR"

exec "$ROOT/packaging/appimage/build.sh" \
    --rocm \
    --output "$OUT_DIR/Blurb-rocm-x86_64.AppImage" \
    "$@"
