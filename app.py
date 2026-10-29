"""Single entry point for Blurb.

The AppImage has one executable, so the manager, the pull-worker, and the
yt-dlp shim all re-enter this file with a mode flag. Running it with no
arguments (or from a source checkout) opens the manager GUI.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import paths


def _print_versions() -> None:
    import platform

    import ctranslate2
    import faster_whisper
    import transcribe

    print("Blurb self-test")
    print(f"  frozen:        {paths.is_frozen()}")
    print(f"  python:        {sys.version.split()[0]} ({platform.machine()})")
    print(f"  app dir:       {paths.app_dir()}")
    print(f"  config dir:    {paths.config_dir()}")
    print(f"  config file:   {paths.env_file()}")
    print(f"  backend:       {os.environ.get('BLURB_BACKEND', 'cpu/none')}")
    print(f"  faster-whisper: {faster_whisper.__version__}")
    print(f"  ctranslate2:   {ctranslate2.__version__}")

    try:
        gpus = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        gpus = f"error: {exc}"
    print(f"  GPU devices:   {gpus} (CUDA/HIP)")

    if isinstance(gpus, int) and gpus > 0:
        try:
            print(f"  CUDA compute:  {ctranslate2.get_supported_compute_types('cuda')}")
        except Exception as exc:
            print(f"  CUDA compute:  error: {exc}")

    for name in ("ffmpeg", "ffprobe", "node"):
        print(f"  {name + ':':<14} {shutil.which(name) or 'not found'}")
    try:
        print(f"  ffmpeg (engine): {transcribe.ffmpeg_binary()}")
    except Exception as exc:
        print(f"  ffmpeg (engine): error: {exc}")

    try:
        import discovery

        ytdlp = discovery.yt_dlp_path()
        print(f"  yt-dlp:        {ytdlp}")
        out = subprocess.run([ytdlp, "--version"], capture_output=True, text=True, timeout=30)
        print(f"  yt-dlp version: {(out.stdout or out.stderr).strip()[:80]}")
    except Exception as exc:
        print(f"  yt-dlp:        error: {exc}")

    print(f"  LD_LIBRARY_PATH: {os.environ.get('LD_LIBRARY_PATH', '')}")


def _run_gui() -> None:
    import tkinter as tk

    from blurb_manager import BlurbManager

    root = tk.Tk()
    BlurbManager(root)
    root.mainloop()


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] == "--worker":
        import web_worker

        web_worker.run()
        return

    if args and args[0] == "--yt-dlp":
        # yt-dlp is invoked as a subprocess by discovery.py/download.py.
        sys.argv = ["yt-dlp", *args[1:]]
        from yt_dlp import main as yt_dlp_main

        raise SystemExit(yt_dlp_main())

    if args and args[0] == "--self-test":
        _print_versions()
        return

    if args and args[0] == "--transcribe":
        if len(args) < 2:
            raise SystemExit("usage: blurb --transcribe AUDIO_FILE")
        from transcribe import transcribe_audio

        result = transcribe_audio(Path(args[1]).read_bytes())
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args and args[0] in ("--version", "-V"):
        print("Blurb 0.1.0")
        return

    _run_gui()


if __name__ == "__main__":
    main()
