"""Per-user runtime/cache paths.

Status and yt-dlp cache files used to live at predictable paths in the
world-writable /tmp directory. A local attacker could pre-create a symlink
there and have the worker overwrite/read an unintended file, so keep them in
the user's runtime/cache directory instead.
"""

import os
from pathlib import Path


def _user_cache_dir() -> Path:
    base = Path.home() / ".cache" / "blurb"
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def status_file() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime)
        if base.is_dir():
            return base / "blurb_worker_status.json"
    return _user_cache_dir() / "worker_status.json"


def yt_dlp_cache_dir() -> Path:
    base = _user_cache_dir() / "yt-dlp"
    base.mkdir(parents=True, exist_ok=True)
    return base
