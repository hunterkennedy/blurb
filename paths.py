"""Per-user runtime, config, and bundled-resource paths.

Status and yt-dlp cache files used to live at predictable paths in the
world-writable /tmp directory. A local attacker could pre-create a symlink
there and have the worker overwrite/read an unintended file, so keep them in
the user's runtime/cache directory instead.

The AppImage is mounted read-only, so anything Blurb writes (.env, worker id,
logs) must live outside the bundle. In a source checkout we keep the old
next-to-the-code behaviour so existing dev setups keep working.
"""

import os
import sys
from pathlib import Path

APP_NAME = "blurb"


def is_frozen() -> bool:
    """True when running from a PyInstaller build (including the AppImage)."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory containing the bundled app (read-only inside an AppImage)."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle)
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """Path to a data file shipped with the app (icon, .env.example, ...)."""
    return app_dir() / name


def _xdg_dir(env_var: str, fallback: str) -> Path:
    base = os.environ.get(env_var)
    root = Path(base) if base else Path.home() / fallback
    return root / APP_NAME


def config_dir() -> Path:
    """Writable per-user config directory.

    ``BLURB_CONFIG_DIR`` overrides it (useful for tests and portable installs).
    """
    override = os.environ.get("BLURB_CONFIG_DIR")
    base = Path(override) if override else _xdg_dir("XDG_CONFIG_HOME", ".config")
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def autostart_file() -> Path:
    """XDG autostart entry for the manager (not inside the blurb config dir)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "autostart" / "blurb.desktop"


def env_file() -> Path:
    """The .env file Blurb reads and writes.

    Frozen builds always use the per-user config dir. A source checkout keeps
    using the .env next to the code, which is what the dev setup has always done.
    """
    if is_frozen() or os.environ.get("BLURB_CONFIG_DIR"):
        return config_dir() / ".env"
    return app_dir() / ".env"


def env_example_file() -> Path | None:
    candidate = resource_path(".env.example")
    return candidate if candidate.is_file() else None


def _user_cache_dir() -> Path:
    base = Path.home() / ".cache" / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base


def cache_dir() -> Path:
    return _user_cache_dir()


def log_file(name: str = "blurb.log") -> Path:
    return _user_cache_dir() / name


def status_file() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime)
        if base.is_dir():
            return base / "blurb_worker_status.json"
    return _user_cache_dir() / "worker_status.json"


def worker_id_file() -> Path:
    """Persistent worker identity (must survive AppImage restarts)."""
    if is_frozen() or os.environ.get("BLURB_CONFIG_DIR"):
        return config_dir() / "worker_id.txt"
    return app_dir() / "worker_id.txt"


def yt_dlp_cache_dir() -> Path:
    base = _user_cache_dir() / "yt-dlp"
    base.mkdir(parents=True, exist_ok=True)
    return base
