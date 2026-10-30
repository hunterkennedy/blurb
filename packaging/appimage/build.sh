#!/bin/bash
# Build Blurb as a self-contained AppImage.
#
# Backends:
#   --cuda (default)  NVIDIA + CPU fallback. Bundles cuBLAS/cuBLASLt; the
#                     NVIDIA driver always comes from the host.
#   --rocm            AMD + ROCm. Swaps in the official CTranslate2 ROCm
#                     wheel and requires ROCm 7.x on the *runtime* host.
#   --cpu             CPU only, no GPU runtime libraries.
#
# The convenience wrappers build-nvidia.sh and build-rocm.sh pin the backend
# and output name. Docker uses Ubuntu 22.04 (glibc 2.35) as the build
# environment so the AppImage runs on more distributions.
#
# Usage:
#   packaging/appimage/build-nvidia.sh
#   packaging/appimage/build-rocm.sh
#   packaging/appimage/build.sh --cpu
#   packaging/appimage/build.sh --output /tmp/Blurb.AppImage
#
# Build it on the oldest glibc you want to support.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BUILD="$ROOT/build/appimage"
CACHE="$ROOT/build/cache"
APPIDIR="$BUILD/AppDir"
DIST="$BUILD/dist"
VENV="$ROOT/build/venv-appimage"
PYTHON="${PYTHON:-python3.12}"

BACKEND="${BACKEND:-cuda}"
OUTPUT="$ROOT/dist/Blurb-x86_64.AppImage"

CUBLAS_VERSION="${CUBLAS_VERSION:-12.4.5.8}"
CT2_ROCM_VERSION="${CT2_ROCM_VERSION:-}"  # empty = match installed CTranslate2
NODE_VERSION="${NODE_VERSION:-22.20.0}"
FFMPEG_URL="${FFMPEG_URL:-https://johnvansickle.com/ffmpeg/releases/ffmpeg-7.0.2-amd64-static.tar.xz}"
FFMPEG_SHA256="${FFMPEG_SHA256:-abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67}"
APPIMAGETOOL_URL="${APPIMAGETOOL_URL:-https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage}"
APPIMAGETOOL_SHA256="${APPIMAGETOOL_SHA256:-ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0}"

while [ $# -gt 0 ]; do
    case "$1" in
        --cpu) BACKEND=cpu ;;
        --cuda|--nvidia) BACKEND=cuda ;;
        --rocm) BACKEND=rocm ;;
        --output) OUTPUT="$2"; shift ;;
        -h|--help)
            sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

case "$BACKEND" in
    cuda|rocm|cpu) ;;
    *) echo "unknown backend: $BACKEND (use cuda, rocm, or cpu)" >&2; exit 2 ;;
esac

# Keep direct invocations from accidentally labelling a ROCm/CPU image as the
# default NVIDIA artifact. Wrappers pass --output explicitly.
if [ "$OUTPUT" = "$ROOT/dist/Blurb-x86_64.AppImage" ]; then
    case "$BACKEND" in
        rocm) OUTPUT="$ROOT/dist/Blurb-rocm-x86_64.AppImage" ;;
        cpu)  OUTPUT="$ROOT/dist/Blurb-cpu-x86_64.AppImage" ;;
    esac
fi

if [ "$(uname -m)" != "x86_64" ]; then
    echo "This build script currently targets x86_64 (CUDA/ROCm and AppImage tooling)." >&2
    exit 1
fi

mkdir -p "$BUILD" "$CACHE" "$ROOT/dist"

download() {
    local url="$1" out="$2" sha="${3:-}"
    if [ -n "$sha" ] && [ -f "$out" ] \
        && ! echo "$sha  $out" | sha256sum -c - >/dev/null 2>&1; then
        rm -f "$out"
    fi
    if [ ! -f "$out" ]; then
        echo "    downloading $(basename "$out")"
        curl -fL --retry 3 -o "$out.part" "$url"
        mv "$out.part" "$out"
    fi
    if [ -n "$sha" ]; then
        echo "$sha  $out" | sha256sum -c - >/dev/null || {
            echo "checksum mismatch: $out" >&2
            exit 1
        }
    fi
}

echo "==> Creating build environment"
if [ ! -x "$VENV/bin/python" ]; then
    "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip setuptools wheel
"$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt" "pyinstaller==6.16.0"

echo "==> Running PyInstaller"
rm -rf "$DIST" "$APPIDIR"
"$VENV/bin/pyinstaller" \
    --noconfirm --clean \
    --distpath "$DIST" \
    --workpath "$BUILD/pyinstaller" \
    "$ROOT/packaging/blurb.spec" >/dev/null

echo "==> Assembling AppDir"
mkdir -p \
    "$APPIDIR/usr/bin" \
    "$APPIDIR/usr/lib/blurb/bin" \
    "$APPIDIR/usr/lib/blurb/node/bin" \
    "$APPIDIR/usr/lib/blurb/cuda" \
    "$APPIDIR/usr/share/applications" \
    "$APPIDIR/usr/share/icons/hicolor/512x512/apps"

cp -a "$DIST/blurb/." "$APPIDIR/usr/bin/"

install -m 0755 "$ROOT/packaging/appimage/AppRun" "$APPIDIR/AppRun"
install -m 0755 "$ROOT/packaging/appimage/blurb-yt-dlp" "$APPIDIR/usr/bin/blurb-yt-dlp"
install -m 0644 "$ROOT/packaging/appimage/blurb.desktop" "$APPIDIR/blurb.desktop"
install -m 0644 "$ROOT/packaging/appimage/blurb.desktop" "$APPIDIR/usr/share/applications/blurb.desktop"
install -m 0644 "$ROOT/blurb.png" "$APPIDIR/blurb.png"
install -m 0644 "$ROOT/blurb.png" "$APPIDIR/usr/share/icons/hicolor/512x512/apps/blurb.png"
ln -sf blurb.png "$APPIDIR/.DirIcon"

# The PyQt6 hook follows the GTK platform theme plugin and drags in a full GTK
# stack. Blurb only needs Qt's Fusion style for the manager window and tray, so
# drop that theme (Qt falls back to Fusion) along with Qt features the app never
# uses. This saves ~30 MB and avoids depending on host GTK libraries.
INTERNAL="$APPIDIR/usr/bin/_internal"
QT="$INTERNAL/PyQt6/Qt6"
rm -rf "$QT/translations" \
       "$QT/plugins/imageformats" \
       "$QT/plugins/iconengines" \
       "$QT/plugins/generic" \
       "$QT/plugins/assetimporters" \
       "$QT/plugins/platformthemes" \
       "$QT/plugins/geometryloaders" \
       "$QT/plugins/sceneparsers" \
       "$QT/plugins/renderers" \
       "$QT/plugins/wayland-decoration-client"
rm -f "$QT/lib/libQt6Pdf.so.6" "$QT/lib/libQt6Svg.so.6"
rm -f "$INTERNAL"/libgtk-3.so.0 \
      "$INTERNAL"/libgdk-3.so.0 \
      "$INTERNAL"/libgdk_pixbuf-2.0.so.0 \
      "$INTERNAL"/libatk-1.0.so.0 \
      "$INTERNAL"/libatk-bridge-2.0.so.0 \
      "$INTERNAL"/libglycin-2.so.0 \
      "$INTERNAL"/libcairo.so.2 \
      "$INTERNAL"/libcairo-gobject.so.2 \
      "$INTERNAL"/libpango-1.0.so.0 \
      "$INTERNAL"/libpangocairo-1.0.so.0 \
      "$INTERNAL"/libpangoft2-1.0.so.0 \
      "$INTERNAL"/libjpeg-*.so.* \
      "$INTERNAL"/libtiff-*.so.* \
      "$INTERNAL"/libwebp-*.so.* \
      "$INTERNAL"/libwebpdemux-*.so.* \
      "$INTERNAL"/libwebpmux-*.so.*

# PyInstaller treats these as system libraries, but the target distro may not
# ship the exact sonames (Fedora has neither libpcre.so.3 nor libbz2.so.1.0).
# libglib is pulled in by QtCore, and Python's _bz2 extension is used by
# yt-dlp/urllib for some compressed responses.
for lib in libbz2.so.1.0 libpcre.so.3; do
    src="$(find /lib /usr/lib -name "$lib" -print -quit 2>/dev/null || true)"
    if [ -n "$src" ]; then
        install -m 0755 "$src" "$INTERNAL/$lib"
    fi
done

if [ "$BACKEND" = "rocm" ]; then
    echo "==> Swapping in the official CTranslate2 ROCm wheel"
    if [ -z "$CT2_ROCM_VERSION" ]; then
        CT2_ROCM_VERSION="$("$VENV/bin/python" -c 'import ctranslate2; print(ctranslate2.__version__)')"
    fi
    ROCM_ZIP="$CACHE/rocm/rocm-python-wheels-${CT2_ROCM_VERSION}.zip"
    mkdir -p "$CACHE/rocm"
    if [ ! -f "$ROCM_ZIP" ]; then
        curl -fL --retry 3 -o "$ROCM_ZIP" \
            "https://github.com/OpenNMT/CTranslate2/releases/download/v${CT2_ROCM_VERSION}/rocm-python-wheels-Linux.zip"
    fi

    TMP="$CACHE/rocm-extract"
    rm -rf "$TMP"
    mkdir -p "$TMP"
    "$VENV/bin/python" -m zipfile -e "$ROCM_ZIP" "$TMP"

    PYTAG="$("$VENV/bin/python" -c 'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")')"
    WHEEL="$(find "$TMP" -name "ctranslate2-${CT2_ROCM_VERSION}-${PYTAG}-${PYTAG}-*.whl" | head -1)"
    if [ -z "$WHEEL" ]; then
        echo "No ROCm wheel for $PYTAG in $ROCM_ZIP" >&2
        exit 1
    fi

    WHEEL_DIR="$TMP/wheel"
    rm -rf "$WHEEL_DIR"
    mkdir -p "$WHEEL_DIR"
    "$VENV/bin/python" -m zipfile -e "$WHEEL" "$WHEEL_DIR"

    # Replace the CUDA/CPU CTranslate2 build that PyInstaller collected. The
    # Python package name and ABI are identical; only the shared library
    # backend differs.
    rm -rf "$INTERNAL/ctranslate2" "$INTERNAL/ctranslate2.libs"
    cp -a "$WHEEL_DIR/ctranslate2" "$INTERNAL/ctranslate2"
    cp -a "$WHEEL_DIR/ctranslate2.libs" "$INTERNAL/ctranslate2.libs"
    rm -f "$INTERNAL"/libctranslate2-*.so* "$INTERNAL"/libomp-*.so*

    # ROCm 7 host libraries require a newer libstdc++ (GLIBCXX_3.4.32) than
    # the Ubuntu 22.04 build environment ships. Keeping the bundled copy first
    # in LD_LIBRARY_PATH makes libamdhip64 fail to load. Use the host's newer
    # libstdc++ instead; the ROCm image targets modern ROCm 7 distros.
    rm -f "$INTERNAL/libstdc++.so.6" "$INTERNAL/libgcc_s.so.1"

    # AppRun uses this marker to add the host's ROCm 7 libraries to the loader
    # path. ROCm is intentionally not bundled: it is large, versioned, and
    # tied to the host kernel driver.
    mkdir -p "$APPIDIR/usr/lib/blurb/rocm"
    echo "    CTranslate2 $CT2_ROCM_VERSION (ROCm) installed; runtime host needs ROCm 7.x"
fi

if [ "$BACKEND" = "cuda" ]; then
    echo "==> Bundling cuBLAS $CUBLAS_VERSION (CUDA runtime, not the driver)"
    mkdir -p "$CACHE/cublas"
    if ! ls "$CACHE/cublas"/nvidia_cublas_cu12-*.whl >/dev/null 2>&1; then
        "$VENV/bin/python" -m pip download \
            --quiet --no-deps --dest "$CACHE/cublas" \
            "nvidia-cublas-cu12==$CUBLAS_VERSION"
    fi
    WHEEL="$(ls "$CACHE/cublas"/nvidia_cublas_cu12-*.whl | head -1)"
    TMP="$CACHE/cublas-extract"
    rm -rf "$TMP"
    mkdir -p "$TMP"
    "$VENV/bin/python" -m zipfile -e "$WHEEL" "$TMP"
    install -m 0755 "$TMP/nvidia/cublas/lib/libcublas.so.12" \
        "$APPIDIR/usr/lib/blurb/cuda/libcublas.so.12"
    install -m 0755 "$TMP/nvidia/cublas/lib/libcublasLt.so.12" \
        "$APPIDIR/usr/lib/blurb/cuda/libcublasLt.so.12"
else
    rmdir "$APPIDIR/usr/lib/blurb/cuda" 2>/dev/null || true
fi

echo "==> Bundling static ffmpeg/ffprobe"
mkdir -p "$CACHE/ffmpeg"
if [ ! -x "$CACHE/ffmpeg/ffmpeg" ]; then
    download "$FFMPEG_URL" "$CACHE/ffmpeg.tar.xz" "$FFMPEG_SHA256"
    tar -xJf "$CACHE/ffmpeg.tar.xz" -C "$CACHE/ffmpeg" --strip-components=1
fi
install -m 0755 "$CACHE/ffmpeg/ffmpeg" "$APPIDIR/usr/lib/blurb/bin/ffmpeg"
install -m 0755 "$CACHE/ffmpeg/ffprobe" "$APPIDIR/usr/lib/blurb/bin/ffprobe"

echo "==> Bundling Node.js v$NODE_VERSION"
mkdir -p "$CACHE/node"
NODE_DIR="$CACHE/node/node-v$NODE_VERSION-linux-x64"
if [ ! -x "$NODE_DIR/bin/node" ]; then
    download "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" \
        "$CACHE/node.tar.xz"
    tar -xJf "$CACHE/node.tar.xz" -C "$CACHE/node"
fi
install -m 0755 "$NODE_DIR/bin/node" "$APPIDIR/usr/lib/blurb/node/bin/node"

echo "==> Building AppImage"
TOOLS="$ROOT/build/tools"
mkdir -p "$TOOLS"
APPIMAGETOOL="$TOOLS/appimagetool"
if [ ! -x "$APPIMAGETOOL" ]; then
    download "$APPIMAGETOOL_URL" "$APPIMAGETOOL" "$APPIMAGETOOL_SHA256"
    chmod +x "$APPIMAGETOOL"
fi

mkdir -p "$(dirname "$OUTPUT")"
TMP_OUTPUT="${OUTPUT}.tmp"
rm -f "$TMP_OUTPUT"
ARCH=x86_64 "$APPIMAGETOOL" --appimage-extract-and-run --no-appstream \
    "$APPIDIR" "$TMP_OUTPUT"

# Replacing a mounted AppImage fails with a cryptic "Text file busy". Build to
# a temp path first so a failed replace leaves the new image intact and gives
# a clear message.
if ! mv -f "$TMP_OUTPUT" "$OUTPUT"; then
    echo "Could not replace $OUTPUT — is it currently mounted or running?" >&2
    echo "Close it and retry. The new image is at $TMP_OUTPUT." >&2
    exit 1
fi

echo
echo "Built: $OUTPUT"
du -h "$OUTPUT"
