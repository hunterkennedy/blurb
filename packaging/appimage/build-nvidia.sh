#!/bin/bash
# Build the NVIDIA/CPU AppImage.
#
# Output: dist/Blurb-nvidia-x86_64.AppImage
# Requires no NVIDIA GPU at build time. The image bundles cuBLAS and uses the
# host's NVIDIA driver at runtime; machines without an NVIDIA GPU run on CPU.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR="${BLURB_OUTPUT_DIR:-$ROOT/dist}"
mkdir -p "$OUT_DIR"

exec "$ROOT/packaging/appimage/build.sh" \
    --cuda \
    --output "$OUT_DIR/Blurb-nvidia-x86_64.AppImage" \
    "$@"
