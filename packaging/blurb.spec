# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Blurb AppImage.

Build from the repository root with:

    pyinstaller --noconfirm --clean packaging/blurb.spec

The output is a one-directory bundle in ``dist/blurb``; the AppImage build
script wraps it in an AppDir and adds ffmpeg/node/CUDA runtime files.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "blurb.png"), "."),
    (str(ROOT / ".env.example"), "."),
]
binaries = []
hiddenimports = [
    # Imported lazily by web_worker.py, so PyInstaller cannot see them.
    "discovery",
    "download",
    "transcribe",
    "errors",
    "test_mode",
    "paths",
]

# Packages with data files, bundled shared libraries, or dynamically imported
# submodules (yt-dlp extractors in particular).
for package in (
    "faster_whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "yt_dlp",
    "yt_dlp_ejs",
    "onnxruntime",
    "huggingface_hub",
    "certifi",
    "defusedxml",
):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception as exc:  # pragma: no cover - build-time diagnostics
        print(f"warning: could not collect {package}: {exc}")
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # torch is only needed by CTranslate2's offline model converter, which
    # Blurb never uses. Excluding it keeps the AppImage several GB smaller and
    # avoids a second, conflicting CUDA runtime in the bundle.
    excludes=[
        "torch",
        "torchvision",
        "torchaudio",
        "fastapi",
        "uvicorn",
        "starlette",
        "pydantic",
        "sympy",
        "networkx",
        "matplotlib",
        "IPython",
        "pytest",
        "uvloop",
        "httptools",
        "watchfiles",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="blurb",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="blurb",
)
