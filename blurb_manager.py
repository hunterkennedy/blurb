"""
Blurb Manager — control panel for the Blurb pull-worker.

Manages web_worker.py as a background process and shows live status. Runs a
single Qt event loop for both the window and the system tray, so status,
progress, and the tray icon stay in sync without cross-toolkit plumbing.

Auto-started on login via ~/.config/autostart/blurb.desktop.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PyQt6.QtCore import (
    QObject,
    QSettings,
    QSignalBlocker,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import paths

BLURB_DIR   = paths.app_dir()
VENV_PYTHON = BLURB_DIR / "venv-linux" / "bin" / "python"
STATUS_FILE = paths.status_file()
POLL_MS     = 1000

# Catppuccin Mocha-ish palette. Kept close to the original Tk colours so the
# app still feels like Blurb, but with enough contrast for small text.
COLOR_BG        = "#1e1e2e"
COLOR_CARD      = "#313244"
COLOR_CARD_DARK = "#262637"
COLOR_BORDER    = "#45475a"
COLOR_FG        = "#cdd6f4"
COLOR_MUTED     = "#a6adc8"
COLOR_SUBTLE    = "#9ca3bb"
COLOR_RUN       = "#a6e3a1"
COLOR_WORK      = "#89b4fa"
COLOR_WARN      = "#f9e2af"
COLOR_STOP      = "#f38ba8"
COLOR_ACCENT    = "#89b4fa"
COLOR_BTN_FG    = "#1e1e2e"

# Human labels for the job kinds web_worker.py reports as `state`.
_STATE_LABELS = {"discover": "Discovering", "process": "Working"}

# Per-stage labels inside a process job.
_STAGE_LABELS = {
    "claimed": "Preparing",
    "downloading": "Downloading audio",
    "transcribing": "Transcribing",
    "probing duration": "Reading metadata",
    "finishing": "Saving",
    "discovering": "Discovering",
}

# Flag a cookie file as "rotate soon" once it's within this many days of its
# earliest-expiring persistent cookie.
_COOKIE_ROTATE_WARNING_DAYS = 7

_APP_QSS = f"""
QWidget {{
    color: {COLOR_FG};
    font-family: "Inter", "Noto Sans", "Cantarell", "DejaVu Sans", sans-serif;
    font-size: 13px;
}}
QMainWindow, QWidget#Root {{
    background: {COLOR_BG};
}}
QLabel#H1 {{
    font-size: 21px;
    font-weight: 600;
}}
QLabel#H2 {{
    font-size: 14px;
    font-weight: 600;
}}
QLabel#Status {{
    font-size: 21px;
    font-weight: 600;
}}
QLabel#Muted {{
    color: {COLOR_MUTED};
}}
QLabel#Subtle {{
    color: {COLOR_SUBTLE};
    font-size: 12px;
}}
QLabel#FieldLabel {{
    color: {COLOR_MUTED};
    font-size: 12px;
    font-weight: 500;
}}
QLabel#ErrorText {{
    color: {COLOR_STOP};
    font-size: 12px;
}}
QFrame#Card {{
    background: {COLOR_CARD};
    border: 1px solid #3b3d52;
    border-radius: 12px;
}}
QFrame#Warning {{
    background: #3a3226;
    border: 1px solid #5c4d2f;
    border-radius: 10px;
}}
QFrame#DemoBar {{
    background: #2a2438;
    border-bottom: 1px solid {COLOR_BORDER};
}}
QLabel#DemoBadge {{
    background: {COLOR_WARN};
    color: {COLOR_BTN_FG};
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 700;
}}
QPushButton {{
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 9px 14px;
    font-weight: 600;
}}
QPushButton#Primary {{
    background: {COLOR_RUN};
    color: {COLOR_BTN_FG};
}}
QPushButton#Primary:hover {{
    background: #b8ecb4;
}}
QPushButton#Primary:pressed {{
    background: #8fd28b;
}}
QPushButton#Danger {{
    background: {COLOR_STOP};
    color: {COLOR_BTN_FG};
}}
QPushButton#Danger:hover {{
    background: #f7a8bd;
}}
QPushButton#Danger:pressed {{
    background: #e0708f;
}}
QPushButton#Secondary {{
    background: #3b3d52;
    color: {COLOR_FG};
}}
QPushButton#Secondary:hover {{
    background: {COLOR_BORDER};
}}
QPushButton#Secondary:pressed {{
    background: #585b70;
}}
QPushButton#Ghost {{
    background: transparent;
    color: {COLOR_MUTED};
    padding: 6px 8px;
}}
QPushButton#Ghost:hover {{
    background: {COLOR_CARD};
    color: {COLOR_FG};
}}
QPushButton:disabled {{
    background: #2a2b3c;
    color: #585b70;
}}
QPushButton:focus {{
    border: 1px solid {COLOR_ACCENT};
}}
QPushButton#Primary:disabled {{
    background: #2a2b3c;
    color: #585b70;
}}
QPushButton#Danger:disabled {{
    background: #2a2b3c;
    color: #585b70;
}}
QPushButton#Secondary:disabled {{
    background: #2a2b3c;
    color: #585b70;
}}
QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 5px 8px;
    color: {COLOR_MUTED};
    font-size: 17px;
}}
QToolButton:hover {{
    background: {COLOR_CARD};
    color: {COLOR_FG};
}}
QToolButton:focus {{
    border: 1px solid {COLOR_ACCENT};
}}
QLineEdit, QComboBox {{
    background: {COLOR_CARD_DARK};
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: {COLOR_ACCENT};
    selection-color: {COLOR_BTN_FG};
}}
QLineEdit:focus, QComboBox:focus {{
    border-color: {COLOR_ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled {{
    color: #585b70;
}}
QComboBox {{
    padding-right: 26px;
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: none;
}}
QComboBox QAbstractItemView {{
    background: {COLOR_CARD_DARK};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: {COLOR_ACCENT};
    selection-color: {COLOR_BTN_FG};
    outline: 0;
}}
QCheckBox {{
    spacing: 8px;
    color: {COLOR_FG};
}}
QCheckBox:focus {{
    color: {COLOR_ACCENT};
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #585b70;
    background: {COLOR_CARD_DARK};
}}
QCheckBox::indicator:hover {{
    border-color: {COLOR_ACCENT};
}}
QCheckBox::indicator:checked {{
    background: {COLOR_ACCENT};
    border-color: {COLOR_ACCENT};
}}
QProgressBar {{
    background: {COLOR_CARD_DARK};
    border: none;
    border-radius: 4px;
    min-height: 8px;
    max-height: 8px;
}}
QProgressBar::chunk {{
    background: {COLOR_WORK};
    border-radius: 4px;
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollArea > QWidget,
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {COLOR_BORDER};
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: #585b70;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QPlainTextEdit {{
    background: #181825;
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    padding: 8px;
    selection-background-color: {COLOR_ACCENT};
    selection-color: {COLOR_BTN_FG};
}}
QMenu {{
    background: {COLOR_CARD};
    border: 1px solid {COLOR_BORDER};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 18px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background: {COLOR_BORDER};
}}
QMenu::separator {{
    height: 1px;
    background: {COLOR_BORDER};
    margin: 4px 8px;
}}
QToolTip {{
    background: {COLOR_CARD};
    color: {COLOR_FG};
    border: 1px solid {COLOR_BORDER};
    padding: 4px 6px;
}}
"""


# ============================================================================
# Config, status, and cookie helpers
# ============================================================================

def _read_env_var(key: str) -> str:
    env_path = paths.env_file()
    if not env_path.exists():
        return ""
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return ""
    return ""


def _read_env_bool(key: str, default: bool) -> bool:
    raw = _read_env_var(key).strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _write_env_vars(updates: dict) -> None:
    """Update (or append) key=value lines in .env, preserving everything else.

    Falls back to .env.example as a starting point if .env doesn't exist yet
    (matches what scripts/dev-install.sh does on first setup). The file holds
    the API key, so keep it private to the user.
    """
    env_path = paths.env_file()
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    else:
        example = paths.env_example_file()
        lines = example.read_text().splitlines() if example else []

    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for key in list(remaining):
            if stripped.startswith(f"{key}="):
                lines[i] = f"{key}={remaining.pop(key)}"
                break

    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass


def _desktop_exec_quote(value: str) -> str:
    """Quote one argument for a .desktop Exec line (not shell quoting)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _autostart_command() -> str:
    """Command a desktop session should run to open the manager."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return _desktop_exec_quote(appimage)
    if paths.is_frozen():
        # APPIMAGE should be set for a real AppImage; this is a fallback.
        return _desktop_exec_quote(sys.executable)
    return " ".join((
        _desktop_exec_quote(sys.executable),
        _desktop_exec_quote(str(paths.app_dir() / "blurb_manager.py")),
    ))


def _autostart_enabled() -> bool:
    return paths.autostart_file().is_file()


def _set_autostart(enabled: bool) -> None:
    path = paths.autostart_file()
    if not enabled:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([
            "[Desktop Entry]",
            "Type=Application",
            "Name=Blurb",
            "Comment=GPU-accelerated audio transcription worker",
            f"Exec={_autostart_command()}",
            "Icon=blurb",
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            "",
        ])
    )
    try:
        path.chmod(0o644)
    except OSError:
        pass


def _read_worker_status() -> dict:
    try:
        data = json.loads(STATUS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_cookie_expiry(path: Path) -> float | None:
    """Earliest expiration timestamp among persistent cookies in a Netscape
    cookies.txt file, or None if the file has no persistent (non-session)
    cookies or can't be parsed."""
    earliest = None
    try:
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 7:
                continue
            try:
                expiry = float(fields[4])
            except ValueError:
                continue
            if expiry <= 0:
                continue  # session cookie — no fixed expiry to track
            if earliest is None or expiry < earliest:
                earliest = expiry
    except Exception:
        return None
    return earliest


def _cookie_status(env_key: str) -> dict:
    """Presence/expiry status for a *_COOKIES_FILE env var.

    Returns {"state": "unset" | "missing" | "unknown" | "expired" | "expiring" | "ok",
             "expires_at": float | None, "path": str}
    """
    path_str = _read_env_var(env_key)
    if not path_str:
        return {"state": "unset", "expires_at": None, "path": ""}

    path = Path(path_str).expanduser()
    if not path.exists():
        return {"state": "missing", "expires_at": None, "path": path_str}

    expiry = _parse_cookie_expiry(path)
    if expiry is None:
        return {"state": "unknown", "expires_at": None, "path": path_str}

    now = time.time()
    if expiry <= now:
        return {"state": "expired", "expires_at": expiry, "path": path_str}
    if expiry <= now + _COOKIE_ROTATE_WARNING_DAYS * 86400:
        return {"state": "expiring", "expires_at": expiry, "path": path_str}
    return {"state": "ok", "expires_at": expiry, "path": path_str}


def _cookie_words(status: dict) -> tuple[str, str]:
    """Short status text + colour for a cookie file, for the settings page."""
    state = status["state"]
    expires_at = status.get("expires_at")
    date_str = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d") if expires_at else ""

    if state == "unset":
        return "Not configured", COLOR_SUBTLE
    if state == "missing":
        return "File not found", COLOR_STOP
    if state == "unknown":
        return "Configured (no expiry found)", COLOR_RUN
    if state == "expired":
        return f"Expired {date_str}", COLOR_STOP
    if state == "expiring":
        days = max(0, math.ceil((expires_at - time.time()) / 86400))
        day_word = "day" if days == 1 else "days"
        return f"Expires in {days} {day_word} ({date_str})", COLOR_WARN
    return f"Valid until {date_str}", COLOR_RUN


def _cookie_issue(label: str, status: dict) -> str | None:
    """One-line warning for the dashboard, or None when nothing is wrong."""
    state = status["state"]
    expires_at = status.get("expires_at")
    if state == "missing":
        return f"{label} cookie file is missing."
    if state == "expired":
        date_str = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d") if expires_at else ""
        return f"{label} cookies expired {date_str}."
    if state == "expiring":
        days = max(0, math.ceil((expires_at - time.time()) / 86400))
        day_word = "day" if days == 1 else "days"
        return f"{label} cookies expire in {days} {day_word}."
    return None


def _shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _url_host(url: str) -> str:
    try:
        return urlsplit(url).netloc or url
    except Exception:
        return url


def _relative_time(timestamp: float | None) -> str:
    if not isinstance(timestamp, (int, float)) or not timestamp:
        return ""
    delta = max(0, time.time() - timestamp)
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _format_duration(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _format_countdown(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h"


def _status_title(state: str, stage: str, progress: dict) -> str:
    stage = (stage or (progress or {}).get("stage") or "").strip().lower()
    if state == "discover":
        return "Discovering"
    if state == "process":
        return _STAGE_LABELS.get(stage, "Working")
    return _STATE_LABELS.get(state, "Working")


def _error_hint(error: str) -> str | None:
    """A short, actionable explanation for common worker failures."""
    low = (error or "").lower()
    if "401" in low or "unauthorized" in low:
        return "The API key was rejected. Check it in Settings."
    if "403" in low or "forbidden" in low:
        return "The server refused access. Check the API key and permissions."
    if "404" in low:
        return "The server URL looks wrong — check the path in Settings."
    if "connection refused" in low or "connect error" in low or "failed to connect" in low:
        return "Could not reach the server. Check the URL and your network."
    if "timed out" in low or "timeout" in low:
        return "The server did not respond in time. It may be offline or busy."
    if "cookies" in low:
        return "Open Advanced settings and check the cookie file."
    if "ffmpeg" in low:
        return "FFmpeg is missing. Use the AppImage or install ffmpeg."
    if "no space" in low or "disk" in low:
        return "The disk may be full."
    return None


# ============================================================================
# Small widgets / background helpers
# ============================================================================

def _ensure_check_icon() -> str | None:
    """Dark check for the custom-styled checkbox indicator."""
    path = paths.cache_dir() / "check-dark.png"
    try:
        if not path.exists():
            pixmap = QPixmap(16, 16)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(
                QColor(COLOR_BTN_FG), 2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            ))
            painter.drawLine(4, 8, 7, 11)
            painter.drawLine(7, 11, 12, 5)
            painter.end()
            pixmap.save(str(path), "PNG")
        return path.as_posix()
    except Exception:
        return None


def _ensure_arrow_icon() -> str | None:
    """Chevron for combo boxes; Fusion's default arrow is easy to miss."""
    path = paths.cache_dir() / "combo-arrow.png"
    try:
        if not path.exists():
            pixmap = QPixmap(20, 12)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(
                QColor(COLOR_MUTED), 2,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            ))
            painter.drawLine(4, 4, 10, 9)
            painter.drawLine(10, 9, 16, 4)
            painter.end()
            pixmap.save(str(path), "PNG")
        return path.as_posix()
    except Exception:
        return None


class _Dot(QLabel):
    def __init__(self, size: int = 12, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.set_color(COLOR_SUBTLE)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(f"background: {color}; border-radius: {self._size // 2}px;")


class ElidedLabel(QLabel):
    """A single-line label that elides its text instead of forcing width."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        QLabel.setText(self, text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full_text = text
        self._apply_elide()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = max(0, self.width())
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, width
        )
        QLabel.setText(self, elided)


class _ConnectionTester(QObject):
    """Runs the reachability check off the GUI thread."""

    finished = pyqtSignal(bool, str)

    def test(self, url: str) -> None:
        threading.Thread(target=self._run, args=(url,), daemon=True).start()

    def _run(self, url: str) -> None:
        try:
            response = httpx.get(url, timeout=6.0, follow_redirects=True)
            code = response.status_code
            if code < 400:
                message = f"Server reachable (HTTP {code})."
            elif code in (401, 403):
                message = f"Server reachable (HTTP {code}); credentials not checked."
            else:
                message = f"Server reachable (HTTP {code}) — check the URL path."
            self.finished.emit(True, message)
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            self.finished.emit(False, f"Could not reach server: {_shorten(detail, 120)}")


class LogViewer(QDialog):
    """Read-only, auto-refreshing view of the worker and manager logs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Blurb logs")
        self.resize(820, 560)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("File"))
        self.file_combo = QComboBox()
        self._files = {
            "Worker log": paths.log_file("worker.log"),
            "Worker console": paths.log_file("worker-console.log"),
            "Manager log": paths.log_file("blurb.log"),
        }
        self.file_combo.addItems(list(self._files))
        self.file_combo.currentTextChanged.connect(self._load)
        top.addWidget(self.file_combo)
        top.addStretch(1)

        folder_btn = QPushButton("Open folder")
        folder_btn.setObjectName("Ghost")
        folder_btn.clicked.connect(self._open_folder)
        top.addWidget(folder_btn)

        copy_btn = QPushButton("Copy diagnostics")
        copy_btn.setObjectName("Secondary")
        copy_btn.clicked.connect(self._copy_diagnostics)
        top.addWidget(copy_btn)
        layout.addLayout(top)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        font = QFont("JetBrains Mono", 10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.text.setFont(font)
        layout.addWidget(self.text, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._load)
        self._timer.start()
        self._last_source: tuple[str, int, int] | tuple[str, str] | None = None
        self._load()

    def _read_tail(self, path: Path) -> str:
        """Read at most the last 256 KiB so the 1s refresh stays cheap."""
        size = path.stat().st_size
        tail_bytes = 256 * 1024
        if size > tail_bytes:
            with path.open("rb") as handle:
                handle.seek(size - tail_bytes)
                data = handle.read()
            text = data.decode(errors="replace")
            return "(… earlier lines omitted …)\n" + text
        return path.read_text(errors="replace")

    def _load(self, *_args) -> None:
        path = self._files.get(self.file_combo.currentText())
        if path is None:
            return
        try:
            stat = path.stat()
            source = (str(path), stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            source = (str(path), "missing")
        except OSError as exc:
            source = (str(path), f"error:{exc}")
        if source == self._last_source:
            return
        self._last_source = source

        if isinstance(source[1], str):
            text = "(no log yet)" if source[1] == "missing" else f"(could not read log: {source[1]})"
        else:
            try:
                text = self._read_tail(path)
            except OSError as exc:
                text = f"(could not read log: {exc})"

        lines = text.splitlines()
        if len(lines) > 2000:
            text = "(… earlier lines omitted …)\n" + "\n".join(lines[-2000:]) + "\n"

        scrollbar = self.text.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        old_value = scrollbar.value()
        if self.text.toPlainText() != text:
            self.text.setPlainText(text)
            if at_bottom:
                scrollbar.setValue(scrollbar.maximum())
            else:
                scrollbar.setValue(min(old_value, scrollbar.maximum()))

    def _open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.cache_dir())))

    def _copy_diagnostics(self) -> None:
        status = _read_worker_status()
        summary = [
            "Blurb diagnostics",
            f"config: {paths.env_file()}",
            f"cache:  {paths.cache_dir()}",
            f"status: {STATUS_FILE}",
            "",
            "Worker status:",
            json.dumps(status, indent=2, default=str),
        ]
        QApplication.clipboard().setText("\n".join(summary))


# ============================================================================
# Manager
# ============================================================================

class BlurbManager(QMainWindow):
    def __init__(self, demo: bool = False):
        super().__init__()
        self._demo = demo
        self._demo_scene = "cycle"
        self._demo_scene_started = time.monotonic()
        self._demo_started_at = time.time()
        self._worker_proc: subprocess.Popen | None = None
        self._stopping_proc: subprocess.Popen | None = None
        self._start_error: str | None = None
        self._restarting_until = 0.0
        self._pending_start = False
        self._tick = 0
        self._dirty = False
        self._loading = True
        self._key_visible = False
        self._tested_url: str | None = None
        self._saved_config: dict = {}
        self._saved_autostart = False
        self._settings_check_timer = QTimer(self)
        self._settings_check_timer.setInterval(250)
        self._settings_check_timer.timeout.connect(self._refresh_save_state)
        self._recent_signature = ""
        self._recent_time_labels: list[tuple[QLabel, float]] = []
        self._tray_icon_key = ""
        self._log_dialog: LogViewer | None = None
        self._save_flash_timer = QTimer(self)
        self._save_flash_timer.setSingleShot(True)
        self._save_flash_timer.timeout.connect(lambda: self.save_state_lbl.setText(""))
        self._auto_quit_timer = QTimer(self)
        self._auto_quit_timer.setSingleShot(True)
        self._auto_quit_timer.timeout.connect(self._quit_app)

        self._settings = QSettings("Blurb", "Blurb")
        self._web_url = _read_env_var("WEB_URL")
        self._api_key = _read_env_var("BLURB_API_KEY")
        self._auto_start = _read_env_bool("BLURB_AUTO_START_WORKER", True)
        self._quit_when_idle = _read_env_bool("BLURB_QUIT_WHEN_IDLE", False)
        self._worker_started = demo
        self._auto_quit_scheduled = False
        self._quitting = False
        hb_interval_raw = _read_env_var("HEARTBEAT_INTERVAL") or "60"
        try:
            hb_interval = int(hb_interval_raw)
        except ValueError:
            hb_interval = 60
        self._heartbeat_stale_after = max(30, hb_interval) * 3

        self._tester = _ConnectionTester(self)
        self._tester.finished.connect(self._on_test_finished)

        self._setup_window()
        self._build_ui()
        self._setup_shortcuts()
        self._setup_tray()
        self._load_settings_form()
        self._settings_check_timer.start()

        if self._configured():
            if self._auto_start:
                self._start_worker()
        else:
            self._show_settings()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_MS)
        self._poll_timer.timeout.connect(self._on_tick)
        self._poll_timer.start()
        self._on_tick()

    # ------------------------------------------------------------------ setup

    def _configured(self) -> bool:
        return bool(self._web_url and self._api_key)

    def _setup_window(self) -> None:
        self.setWindowTitle("Blurb")
        self.setWindowIcon(QIcon(str(paths.resource_path("blurb.png"))))
        self.setMinimumSize(400, 480)
        geometry = self._settings.value("window/geometry")
        restored = False
        if geometry is not None:
            restored = self.restoreGeometry(geometry)
        if not restored:
            self.resize(460, 580)
            screen = QApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                self.move(
                    area.center().x() - self.width() // 2,
                    area.center().y() - self.height() // 2,
                )

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.demo_combo: QComboBox | None = None
        if self._demo:
            outer.addWidget(self._build_demo_bar())

        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.stack.addWidget(self._build_dashboard())
        self.stack.addWidget(self._build_settings())

    def _build_demo_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("DemoBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 7, 14, 7)
        layout.setSpacing(8)

        badge = QLabel("DEMO")
        badge.setObjectName("DemoBadge")
        layout.addWidget(badge)

        note = QLabel("simulated data")
        note.setObjectName("Subtle")
        layout.addWidget(note)
        layout.addStretch(1)

        self.demo_combo = QComboBox()
        self.demo_combo.setAccessibleName("Demo scene")
        self.demo_combo.addItems([
            "Auto cycle",
            "Downloading",
            "Transcribing",
            "Waiting for jobs",
            "Error",
            "No jobs",
            "Stopped",
        ])
        self.demo_combo.currentTextChanged.connect(self._on_demo_scene_changed)
        layout.addWidget(self.demo_combo)
        return bar

    def _on_demo_scene_changed(self, text: str) -> None:
        mapping = {
            "Auto cycle": "cycle",
            "Downloading": "download",
            "Transcribing": "transcribe",
            "Waiting for jobs": "idle",
            "Error": "error",
            "No jobs": "empty",
            "Stopped": "stopped",
        }
        self._demo_scene = mapping.get(text, "cycle")
        self._demo_scene_started = time.monotonic()
        self._on_tick()

    def _set_demo_scene(self, scene: str) -> None:
        self._demo_scene = scene
        self._demo_scene_started = time.monotonic()
        if self.demo_combo is not None:
            labels = {
                "cycle": "Auto cycle",
                "download": "Downloading",
                "transcribe": "Transcribing",
                "idle": "Waiting for jobs",
                "error": "Error",
                "empty": "No jobs",
                "stopped": "Stopped",
            }
            with QSignalBlocker(self.demo_combo):
                self.demo_combo.setCurrentText(labels.get(scene, "Auto cycle"))

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self._quit_app)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._show_logs)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self._show_settings)
        QShortcut(QKeySequence("Esc"), self, activated=self._escape)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save_settings)

    def _escape(self) -> None:
        if self.stack.currentIndex() == 1 and self._configured():
            self._show_dashboard()

    # -------------------------------------------------------------- dashboard

    def _build_dashboard(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        # Header
        header = QHBoxLayout()
        header.setSpacing(10)
        logo = QLabel()
        pixmap = QPixmap(str(paths.resource_path("blurb.png")))
        if not pixmap.isNull():
            logo.setPixmap(pixmap.scaled(
                30, 30,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        logo.setFixedSize(30, 30)
        header.addWidget(logo)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("Blurb")
        title.setObjectName("H1")
        subtitle = QLabel("Transcription worker")
        subtitle.setObjectName("Muted")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch(1)

        self.logs_button = QPushButton("Logs")
        self.logs_button.setObjectName("Ghost")
        self.logs_button.setToolTip("View worker logs (Ctrl+L)")
        self.logs_button.clicked.connect(self._show_logs)
        header.addWidget(self.logs_button)

        settings_button = QToolButton()
        settings_button.setText("⚙")
        settings_button.setToolTip("Settings (Ctrl+,)")
        settings_button.setFixedSize(32, 32)
        settings_button.clicked.connect(self._show_settings)
        header.addWidget(settings_button)
        layout.addLayout(header)

        # Status card
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(10)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.status_dot = _Dot(12)
        status_row.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status_lbl = QLabel("Starting…")
        self.status_lbl.setObjectName("Status")
        status_row.addWidget(self.status_lbl)
        status_row.addStretch(1)
        self.site_lbl = QLabel("")
        self.site_lbl.setObjectName("Subtle")
        status_row.addWidget(self.site_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        card_layout.addLayout(status_row)

        self.detail_lbl = QLabel("")
        self.detail_lbl.setObjectName("Muted")
        self.detail_lbl.setWordWrap(True)
        self.detail_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card_layout.addWidget(self.detail_lbl)

        self.error_lbl = QLabel("")
        self.error_lbl.setObjectName("ErrorText")
        self.error_lbl.setWordWrap(True)
        self.error_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error_lbl.hide()
        card_layout.addWidget(self.error_lbl)

        self.error_hint_lbl = QLabel("")
        self.error_hint_lbl.setObjectName("Muted")
        self.error_hint_lbl.setWordWrap(True)
        self.error_hint_lbl.hide()
        card_layout.addWidget(self.error_hint_lbl)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setAccessibleName("Job progress")
        progress_row.addWidget(self.progress, 1)
        self.percent_lbl = QLabel("")
        self.percent_lbl.setObjectName("Subtle")
        self.percent_lbl.setMinimumWidth(38)
        self.percent_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        progress_row.addWidget(self.percent_lbl)
        self.progress_row_widget = QWidget()
        self.progress_row_widget.setLayout(progress_row)
        card_layout.addWidget(self.progress_row_widget)

        conn_row = QHBoxLayout()
        conn_row.setSpacing(7)
        self.conn_dot = _Dot(8)
        conn_row.addWidget(self.conn_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.conn_lbl = QLabel("")
        self.conn_lbl.setObjectName("Subtle")
        conn_row.addWidget(self.conn_lbl)
        conn_row.addStretch(1)
        self.next_lbl = QLabel("")
        self.next_lbl.setObjectName("Subtle")
        conn_row.addWidget(self.next_lbl)
        card_layout.addLayout(conn_row)
        layout.addWidget(card)

        # Actions
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.primary_btn = QPushButton("Stop worker")
        self.primary_btn.setObjectName("Danger")
        self.primary_btn.clicked.connect(self._toggle_worker)
        actions.addWidget(self.primary_btn)
        self.logs_action = QPushButton("View logs")
        self.logs_action.setObjectName("Secondary")
        self.logs_action.clicked.connect(self._show_logs)
        actions.addWidget(self.logs_action)
        actions.addStretch(1)
        layout.addLayout(actions)

        # Cookie warning banner
        self.warning = QFrame()
        self.warning.setObjectName("Warning")
        warning_layout = QHBoxLayout(self.warning)
        warning_layout.setContentsMargins(12, 8, 8, 8)
        warning_layout.setSpacing(8)
        warning_icon = QLabel("⚠")
        warning_icon.setStyleSheet(f"color: {COLOR_WARN}; font-size: 15px;")
        warning_layout.addWidget(warning_icon)
        self.warning_lbl = QLabel("")
        self.warning_lbl.setWordWrap(True)
        self.warning_lbl.setStyleSheet(f"color: {COLOR_WARN};")
        warning_layout.addWidget(self.warning_lbl, 1)
        fix_btn = QPushButton("Fix")
        fix_btn.setObjectName("Ghost")
        fix_btn.setStyleSheet(f"color: {COLOR_WARN};")
        fix_btn.clicked.connect(self._show_settings)
        warning_layout.addWidget(fix_btn)
        self.warning.hide()
        layout.addWidget(self.warning)

        # Recent jobs
        recent_card = QFrame()
        recent_card.setObjectName("Card")
        recent_layout = QVBoxLayout(recent_card)
        recent_layout.setContentsMargins(18, 14, 18, 14)
        recent_layout.setSpacing(8)
        recent_title = QLabel("Recent")
        recent_title.setObjectName("H2")
        recent_layout.addWidget(recent_title)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(6)
        recent_layout.addLayout(self.recent_box)
        layout.addWidget(recent_card)

        layout.addStretch(1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.server_lbl = QLabel("")
        self.server_lbl.setObjectName("Subtle")
        footer.addWidget(self.server_lbl)
        footer.addStretch(1)
        local_lbl = QLabel("Audio is processed locally")
        local_lbl.setObjectName("Subtle")
        footer.addWidget(local_lbl)
        layout.addLayout(footer)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    # --------------------------------------------------------------- settings

    def _build_settings(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        self.back_button = QPushButton("← Dashboard")
        self.back_button.setObjectName("Ghost")
        self.back_button.clicked.connect(self._show_dashboard)
        header.addWidget(self.back_button)
        title = QLabel("Settings")
        title.setObjectName("H1")
        header.addWidget(title)
        header.addStretch(1)
        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 6, 0)
        content_layout.setSpacing(14)

        # Onboarding text (hidden once configured)
        self.onboarding = QWidget()
        onboarding_layout = QVBoxLayout(self.onboarding)
        onboarding_layout.setContentsMargins(0, 0, 0, 0)
        onboarding_layout.setSpacing(4)
        welcome = QLabel("Connect Blurb")
        welcome.setObjectName("H2")
        onboarding_layout.addWidget(welcome)
        welcome_text = QLabel(
            "Paste the server URL and API key from your Blurb server. "
            "The worker will start once the connection is saved."
        )
        welcome_text.setObjectName("Muted")
        welcome_text.setWordWrap(True)
        onboarding_layout.addWidget(welcome_text)
        content_layout.addWidget(self.onboarding)

        # Connection card
        conn_card, conn_layout = self._card("Connection")
        conn_help = QLabel("Where this computer picks up transcription jobs.")
        conn_help.setObjectName("Muted")
        conn_help.setWordWrap(True)
        conn_layout.addWidget(conn_help)

        conn_layout.addWidget(self._field_label("Remote URL"))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://palpal.app/api")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.setAccessibleName("Remote URL")
        conn_layout.addWidget(self.url_edit)

        conn_layout.addWidget(self._field_label("API key"))
        key_row = QHBoxLayout()
        key_row.setSpacing(6)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Shared API key")
        self.key_edit.setAccessibleName("API key")
        key_row.addWidget(self.key_edit, 1)
        self.show_key_btn = QPushButton("Show")
        self.show_key_btn.setObjectName("Secondary")
        self.show_key_btn.setMinimumWidth(64)
        self.show_key_btn.clicked.connect(self._toggle_key_visibility)
        key_row.addWidget(self.show_key_btn)
        conn_layout.addLayout(key_row)

        test_row = QHBoxLayout()
        test_row.setSpacing(8)
        self.test_btn = QPushButton("Test connection")
        self.test_btn.setObjectName("Secondary")
        self.test_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.test_btn.clicked.connect(self._test_connection)
        test_row.addWidget(self.test_btn)
        self.test_lbl = QLabel("")
        self.test_lbl.setObjectName("Subtle")
        self.test_lbl.setWordWrap(True)
        test_row.addWidget(self.test_lbl, 1)
        conn_layout.addLayout(test_row)
        self.test_lbl.hide()
        content_layout.addWidget(conn_card)

        # Startup card
        startup_card, startup_layout = self._card("Startup")
        self.autostart_check = QCheckBox("Launch Blurb at login")
        startup_layout.addWidget(self.autostart_check)
        self.auto_worker_check = QCheckBox("Start the worker automatically")
        startup_layout.addWidget(self.auto_worker_check)
        self.quit_when_idle_check = QCheckBox("Quit when the queue is empty")
        self.quit_when_idle_check.setToolTip(
            "After any queued jobs finish, exit Blurb on the first empty poll "
            "instead of waiting for new work. Network errors do not trigger this."
        )
        startup_layout.addWidget(self.quit_when_idle_check)
        content_layout.addWidget(startup_card)

        # Advanced card
        adv_card, adv_layout = self._card("Advanced")
        adv_help = QLabel(
            "Cookies stay on this machine. They are only read while discovering "
            "or downloading from YouTube/Patreon."
        )
        adv_help.setObjectName("Muted")
        adv_help.setWordWrap(True)
        adv_layout.addWidget(adv_help)

        self.yt_cookie_edit, self.yt_cookie_status = self._cookie_row(
            adv_layout, "YouTube cookies"
        )
        self.patreon_cookie_edit, self.patreon_cookie_status = self._cookie_row(
            adv_layout, "Patreon cookies"
        )

        adv_layout.addWidget(self._field_label("Transcription"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)

        model_label = QLabel("Model")
        model_label.setObjectName("Subtle")
        grid.addWidget(model_label, 0, 0, 1, 2)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setAccessibleName("Whisper model")
        self.model_combo.setToolTip("Faster-Whisper model name. Larger models are more accurate but slower.")
        self.model_combo.addItems([
            "distil-large-v3",
            "large-v3",
            "large-v3-turbo",
            "medium",
            "small",
            "base",
            "tiny",
        ])
        grid.addWidget(self.model_combo, 1, 0, 1, 2)

        device_label = QLabel("Device")
        device_label.setObjectName("Subtle")
        grid.addWidget(device_label, 2, 0)

        compute_label = QLabel("Compute type")
        compute_label.setObjectName("Subtle")
        grid.addWidget(compute_label, 2, 1)

        self.device_combo = QComboBox()
        self.device_combo.setAccessibleName("Whisper device")
        self.device_combo.setToolTip("Auto uses CUDA when available, otherwise CPU.")
        self.device_combo.addItems(["Auto", "CUDA", "CPU"])
        grid.addWidget(self.device_combo, 3, 0)

        self.compute_combo = QComboBox()
        self.compute_combo.setAccessibleName("Whisper compute type")
        self.compute_combo.setToolTip("Auto uses float16 on GPU and int8 on CPU.")
        self.compute_combo.addItems(["Auto", "float16", "int8", "float32"])
        grid.addWidget(self.compute_combo, 3, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        adv_layout.addLayout(grid)
        content_layout.addWidget(adv_card)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        # Footer
        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.settings_error_lbl = QLabel("")
        self.settings_error_lbl.setObjectName("ErrorText")
        self.settings_error_lbl.setWordWrap(True)
        footer.addWidget(self.settings_error_lbl, 1)
        self.save_state_lbl = QLabel("")
        self.save_state_lbl.setObjectName("Subtle")
        footer.addWidget(self.save_state_lbl)
        open_config_btn = QPushButton("Config folder")
        open_config_btn.setObjectName("Ghost")
        open_config_btn.clicked.connect(self._open_config_folder)
        footer.addWidget(open_config_btn)
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("Primary")
        self.save_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.save_btn.clicked.connect(self._save_settings)
        footer.addWidget(self.save_btn)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFixedHeight(1)
        separator.setStyleSheet(f"background: {COLOR_BORDER}; border: none;")
        layout.addWidget(separator)
        layout.addLayout(footer)

        # Wire up dirty tracking
        for line_edit in (
            self.url_edit,
            self.key_edit,
            self.yt_cookie_edit,
            self.patreon_cookie_edit,
        ):
            line_edit.textChanged.connect(self._on_setting_changed)
        for combo in (self.model_combo, self.device_combo, self.compute_combo):
            combo.currentTextChanged.connect(self._on_setting_changed)
        for check in (self.autostart_check, self.auto_worker_check, self.quit_when_idle_check):
            check.toggled.connect(self._on_setting_changed)

        return page

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(9)
        label = QLabel(title)
        label.setObjectName("H2")
        layout.addWidget(label)
        return card, layout

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    def _cookie_row(self, parent_layout: QVBoxLayout, label_text: str):
        parent_layout.addWidget(self._field_label(label_text))
        row = QHBoxLayout()
        row.setSpacing(6)
        entry = QLineEdit()
        entry.setPlaceholderText("~/cookies.txt")
        entry.setAccessibleName(f"{label_text} file")
        entry.setToolTip("Netscape-format cookies.txt. It stays on this machine.")
        row.addWidget(entry, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("Secondary")
        browse.clicked.connect(lambda: self._browse_cookie(entry))
        row.addWidget(browse)
        parent_layout.addLayout(row)
        status = QLabel("")
        status.setObjectName("Subtle")
        status.setWordWrap(True)
        parent_layout.addWidget(status)
        return entry, status

    def _browse_cookie(self, entry: QLineEdit) -> None:
        start = entry.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select cookies file",
            start,
            "Cookies files (*.txt);;All files (*)",
        )
        if path:
            entry.setText(path)

    # ----------------------------------------------------------- settings data

    def _load_settings_form(self) -> None:
        self._loading = True
        self.url_edit.setText(_read_env_var("WEB_URL"))
        self.key_edit.setText(_read_env_var("BLURB_API_KEY"))
        self.yt_cookie_edit.setText(_read_env_var("YOUTUBE_COOKIES_FILE"))
        self.patreon_cookie_edit.setText(_read_env_var("PATREON_COOKIES_FILE"))

        model = _read_env_var("WHISPER_MODEL") or "distil-large-v3"
        self.model_combo.setCurrentText(model)

        device = (_read_env_var("WHISPER_DEVICE") or "auto").lower()
        device_label = {"auto": "Auto", "cuda": "CUDA", "cpu": "CPU"}.get(device, "Auto")
        self.device_combo.setCurrentText(device_label)

        compute = (_read_env_var("WHISPER_COMPUTE_TYPE") or "auto").lower()
        compute_label = {
            "auto": "Auto",
            "float16": "float16",
            "int8": "int8",
            "float32": "float32",
        }.get(compute, "Auto")
        self.compute_combo.setCurrentText(compute_label)

        self.autostart_check.setChecked(_autostart_enabled())
        self.auto_worker_check.setChecked(self._auto_start)
        self.quit_when_idle_check.setChecked(self._quit_when_idle)
        self._loading = False
        self._saved_config = self._collect_settings()
        self._saved_autostart = self.autostart_check.isChecked()
        self._dirty = False
        self.save_btn.setEnabled(False)
        self.save_state_lbl.setText("")
        self.settings_error_lbl.setText("")
        self.onboarding.setVisible(not self._configured())
        self.back_button.setVisible(self._configured())
        self.test_lbl.hide()
        self._refresh_cookie_labels()
        self._refresh_save_state()

    def _collect_settings(self) -> dict:
        device = self.device_combo.currentText().lower()
        if device == "auto":
            device = "auto"
        compute = self.compute_combo.currentText()
        if compute.lower() == "auto":
            compute = ""

        def expanded_path(text: str) -> str:
            text = text.strip()
            return str(Path(text).expanduser().absolute()) if text else ""

        return {
            "WEB_URL": self.url_edit.text().strip().rstrip("/"),
            "BLURB_API_KEY": self.key_edit.text().strip(),
            "YOUTUBE_COOKIES_FILE": expanded_path(self.yt_cookie_edit.text()),
            "PATREON_COOKIES_FILE": expanded_path(self.patreon_cookie_edit.text()),
            "WHISPER_MODEL": self.model_combo.currentText().strip(),
            "WHISPER_DEVICE": device,
            "WHISPER_COMPUTE_TYPE": compute,
            "BLURB_AUTO_START_WORKER": "true" if self.auto_worker_check.isChecked() else "false",
            "BLURB_QUIT_WHEN_IDLE": "true" if self.quit_when_idle_check.isChecked() else "false",
        }

    def _on_setting_changed(self, *_args) -> None:
        if self._loading:
            return
        self._save_flash_timer.stop()
        self.settings_error_lbl.setText("")
        self.test_lbl.hide()
        self._refresh_save_state()

    def _refresh_save_state(self) -> None:
        """Recompute dirty/save state from the widgets.

        Called from every editor signal *and* a light poll, so a missed Qt
        signal or a programmatic value change can't leave Save stuck disabled.
        """
        if self._loading or self.stack.currentIndex() != 1:
            return
        current = self._collect_settings()
        valid = bool(current["WEB_URL"] and current["BLURB_API_KEY"])
        dirty = (
            current != self._saved_config
            or self.autostart_check.isChecked() != self._saved_autostart
        )
        self._dirty = dirty
        self.save_btn.setEnabled(valid and dirty)
        if not valid:
            self.save_btn.setToolTip("Enter both the remote URL and API key first.")
        elif not dirty:
            self.save_btn.setToolTip("No unsaved changes.")
        else:
            self.save_btn.setToolTip("Save changes.")

        if not valid:
            self.save_state_lbl.setText("URL and API key required")
            self.save_state_lbl.setStyleSheet(f"color: {COLOR_WARN};")
        elif dirty:
            self.save_state_lbl.setText("Unsaved changes")
            self.save_state_lbl.setStyleSheet(f"color: {COLOR_WARN};")
        elif self.save_state_lbl.text() == "Unsaved changes":
            self.save_state_lbl.setText("")

    def _validate_url(self) -> str | None:
        url = self.url_edit.text().strip()
        if not url:
            return "Enter the remote URL first."
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return "The remote URL must start with http:// or https:// and include a host."
        return None

    def _validate_settings(self) -> str | None:
        url_error = self._validate_url()
        if url_error:
            return url_error
        if not self.key_edit.text().strip():
            return "Both the remote URL and API key are required."
        return None

    def _save_settings(self) -> None:
        if self.stack.currentIndex() != 1:
            return
        error = self._validate_settings()
        if error:
            self.settings_error_lbl.setText(error)
            return

        updates = self._collect_settings()
        if (
            updates == self._saved_config
            and self.autostart_check.isChecked() == self._saved_autostart
        ):
            self._flash_saved("No changes", 1500)
            return
        try:
            _set_autostart(self.autostart_check.isChecked())
        except OSError as exc:
            self.settings_error_lbl.setText(f"Could not update autostart: {exc}")
            return

        try:
            _write_env_vars(updates)
        except OSError as exc:
            self.settings_error_lbl.setText(f"Could not save settings: {exc}")
            return

        was_configured = self._configured()
        was_alive = self._is_worker_alive()
        was_auto_start = self._auto_start
        self._web_url = updates["WEB_URL"]
        self._api_key = updates["BLURB_API_KEY"]
        self._auto_start = updates["BLURB_AUTO_START_WORKER"] == "true"
        self._quit_when_idle = updates["BLURB_QUIT_WHEN_IDLE"] == "true"
        if self._auto_quit_timer.isActive():
            self._auto_quit_timer.stop()
            self._auto_quit_scheduled = False
            if not was_alive:
                self._clear_exit_reason()

        # Reload from .env so the form shows the normalized values (for
        # example an expanded cookie path) and unsaved state is cleared.
        self._load_settings_form()
        self._key_visible = False
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_key_btn.setText("Show")
        self._flash_saved("Saved")

        if not was_configured:
            self._show_dashboard()

        if was_alive:
            if was_auto_start and not self._auto_start:
                # Turning auto-start off is also an explicit request to stop.
                self._stop_worker()
            else:
                self._restart_worker()
                self._flash_saved("Saved — restarting…", 4000)
        elif self._auto_start:
            self._start_worker()

    def _flash_saved(self, text: str, timeout: int = 2500) -> None:
        self.save_state_lbl.setText(text)
        self.save_state_lbl.setStyleSheet(f"color: {COLOR_RUN};")
        self._save_flash_timer.start(timeout)

    def _toggle_key_visibility(self) -> None:
        self._key_visible = not self._key_visible
        self.key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if self._key_visible else QLineEdit.EchoMode.Password
        )
        self.show_key_btn.setText("Hide" if self._key_visible else "Show")

    def _test_connection(self) -> None:
        error = self._validate_url()
        if error:
            self.test_lbl.setText(error)
            self.test_lbl.setStyleSheet(f"color: {COLOR_STOP};")
            self.test_lbl.show()
            return
        if self._demo:
            self.test_btn.setEnabled(False)
            self.test_btn.setText("Testing…")
            self.test_lbl.setText("Demo mode: checking simulated connection…")
            self.test_lbl.setStyleSheet(f"color: {COLOR_MUTED};")
            self.test_lbl.show()
            self._tested_url = self.url_edit.text().strip().rstrip("/")
            QTimer.singleShot(
                500,
                lambda: self._on_test_finished(
                    True, "Demo mode: server reachable (simulated)."
                ),
            )
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing…")
        self.test_lbl.setText("Checking server reachability…")
        self.test_lbl.setStyleSheet(f"color: {COLOR_MUTED};")
        self.test_lbl.show()
        self._tested_url = self.url_edit.text().strip().rstrip("/")
        self._tester.test(self._tested_url)

    def _on_test_finished(self, ok: bool, message: str) -> None:
        self.test_btn.setEnabled(True)
        self.test_btn.setText("Test connection")
        current = self.url_edit.text().strip().rstrip("/")
        if self._tested_url is not None and current != self._tested_url:
            return
        self.test_lbl.setText(message)
        self.test_lbl.setStyleSheet(f"color: {COLOR_RUN if ok else COLOR_STOP};")
        self.test_lbl.show()

    def _open_config_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.config_dir())))

    # ---------------------------------------------------------------- panels

    def _show_settings(self) -> None:
        self.onboarding.setVisible(not self._configured())
        self.back_button.setVisible(self._configured())
        self._key_visible = False
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_key_btn.setText("Show")
        self.test_lbl.hide()
        self.stack.setCurrentIndex(1)
        self._refresh_save_state()
        self.url_edit.setFocus()

    def _show_dashboard(self) -> None:
        if not self._configured():
            return
        self.stack.setCurrentIndex(0)

    # -------------------------------------------------------- process control

    def _worker_command(self) -> list[str]:
        """How to start the worker in both source and frozen builds."""
        if paths.is_frozen():
            # The AppImage is one executable: re-enter it in worker mode.
            return [sys.executable, "--worker"]
        return [str(VENV_PYTHON), str(BLURB_DIR / "web_worker.py")]

    def _clear_exit_reason(self) -> None:
        """Remove a stale one-shot exit marker before starting a fresh worker."""
        if self._demo:
            return
        try:
            data = json.loads(STATUS_FILE.read_text())
        except Exception:
            return
        if not isinstance(data, dict) or "exit_reason" not in data:
            return
        data.pop("exit_reason", None)
        try:
            STATUS_FILE.write_text(json.dumps(data))
        except OSError:
            pass

    def _start_worker(self) -> None:
        if self._demo:
            self._set_demo_scene("cycle")
            if not self._quitting:
                self._on_tick()
            return
        if self._stopping_proc is not None:
            if self._stopping_proc.poll() is None:
                QTimer.singleShot(200, self._start_worker)
                return
            self._stopping_proc = None
        if not self._configured() or self._is_worker_alive():
            return

        self._clear_exit_reason()
        try:
            log = open(paths.log_file("worker-console.log"), "w", buffering=1)
        except OSError:
            log = subprocess.DEVNULL

        try:
            proc = subprocess.Popen(
                self._worker_command(),
                cwd=str(paths.config_dir() if paths.is_frozen() else BLURB_DIR),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self._start_error = f"Could not start the worker: {exc}"
            proc = None
        else:
            self._start_error = None
        finally:
            if log is not subprocess.DEVNULL:
                log.close()

        self._worker_proc = proc
        if proc is not None:
            self._worker_started = True
            self._auto_quit_scheduled = False
            self._auto_quit_timer.stop()
            self._restarting_until = max(self._restarting_until, time.time() + 1.5)

    def _stop_worker(self) -> None:
        if self._demo:
            self._set_demo_scene("stopped")
            if not self._quitting:
                self._on_tick()
            return
        proc = self._worker_proc
        self._worker_proc = None
        self._pending_start = False
        if proc is None:
            return
        self._stopping_proc = proc
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass

    def _restart_worker(self) -> None:
        if self._demo:
            self._set_demo_scene("cycle")
            if not self._quitting:
                self._on_tick()
            return
        self._restarting_until = time.time() + 8
        self._stop_worker()
        self._pending_start = True
        QTimer.singleShot(250, self._start_when_stopped)

    def _start_when_stopped(self, attempt: int = 0) -> None:
        if not self._pending_start:
            return
        stopping = (
            self._stopping_proc is not None
            and self._stopping_proc.poll() is None
        )
        if self._is_worker_alive() or stopping:
            if attempt < 30:
                QTimer.singleShot(200, lambda: self._start_when_stopped(attempt + 1))
            else:
                self._pending_start = False
                self._start_error = "The worker did not stop in time; try again."
            return
        self._stopping_proc = None
        self._pending_start = False
        self._start_worker()

    def _toggle_worker(self) -> None:
        if not self._configured():
            self._show_settings()
            return
        if self._is_worker_alive():
            self._stop_worker()
        else:
            self._start_worker()

    def _is_worker_alive(self) -> bool:
        if self._demo:
            return self._demo_scene not in ("stopped", "empty")
        proc = self._worker_proc
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        self._worker_proc = None
        return False

    # --------------------------------------------------------- system tray

    def _setup_tray(self) -> None:
        self._tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self._tray = QSystemTrayIcon(self)
        self._tray_menu = QMenu()
        self._tray_status_action = self._tray_menu.addAction("Blurb — stopped")
        self._tray_status_action.setEnabled(False)
        self._tray_menu.addSeparator()
        self._tray_show_action = self._tray_menu.addAction("Show Blurb")
        self._tray_show_action.triggered.connect(self._restore_window)
        self._tray_toggle_action = self._tray_menu.addAction("Start worker")
        self._tray_toggle_action.triggered.connect(self._toggle_worker)
        self._tray_menu.addAction("View logs…").triggered.connect(self._show_logs)
        self._tray_menu.addSeparator()
        self._tray_menu.addAction("Quit").triggered.connect(self._quit_app)

        self._tray.setContextMenu(self._tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.setIcon(self._tray_icon(COLOR_SUBTLE))
        self._tray.setToolTip("Blurb — stopped")
        self._tray.show()

    def _tray_icon(self, color: str) -> QIcon:
        base = QPixmap(str(paths.resource_path("blurb.png")))
        canvas = QPixmap(64, 64)
        canvas.fill(Qt.GlobalColor.transparent)

        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if base.isNull():
            painter.setBrush(QColor(COLOR_CARD))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
            painter.setPen(QColor(COLOR_FG))
            font = QFont("Sans", 26, QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(canvas.rect(), Qt.AlignmentFlag.AlignCenter, "B")
        else:
            scaled = base.scaled(
                64, 64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (64 - scaled.width()) // 2
            y = (64 - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)

        painter.setBrush(QColor(color))
        painter.setPen(QPen(QColor(COLOR_BG), 2))
        painter.drawEllipse(43, 43, 19, 19)
        painter.end()
        return QIcon(canvas)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self._restore_window()

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _update_tray(self, alive: bool, title: str, color: str) -> None:
        if self._tray is None:
            return
        key = f"{alive}:{color}"
        if key != self._tray_icon_key:
            self._tray_icon_key = key
            self._tray.setIcon(self._tray_icon(color))
        self._tray.setToolTip(title)
        self._tray_status_action.setText(title)
        if self._auto_quit_scheduled:
            self._tray_toggle_action.setText("Exiting…")
            self._tray_toggle_action.setEnabled(False)
        elif self._pending_start:
            self._tray_toggle_action.setText("Restarting…")
            self._tray_toggle_action.setEnabled(False)
        elif not self._configured():
            self._tray_toggle_action.setText("Set up Blurb")
            self._tray_toggle_action.setEnabled(True)
        else:
            self._tray_toggle_action.setText("Stop worker" if alive else "Start worker")
            self._tray_toggle_action.setEnabled(True)

    # -------------------------------------------------------------- polling

    def _on_tick(self) -> None:
        self._tick += 1
        if self._demo:
            alive, status = self._demo_frame()
            self._apply_status(alive, status)
            if self._tick % 5 == 0:
                self._refresh_cookie_labels()
            recent = status.get("recent_jobs")
            if not isinstance(recent, list):
                recent = []
            self._update_recent(recent)
            return

        alive = self._is_worker_alive()
        # Keep showing recent history even when the worker is stopped.
        status = _read_worker_status()
        self._maybe_quit_after_empty_queue(alive, status)
        self._apply_status(alive, status)
        if self._tick % 5 == 0:
            self._refresh_cookie_labels()
        recent = status.get("recent_jobs")
        if not isinstance(recent, list):
            recent = []
        self._update_recent(recent)

    def _demo_frame(self) -> tuple[bool, dict]:
        """Build one simulated status snapshot for the demo scene."""
        now = time.time()
        elapsed = max(0.0, time.monotonic() - self._demo_scene_started)
        scene = self._demo_scene
        phase: float | None = None

        if scene == "cycle":
            phase = elapsed % 38.0
            if phase < 10:
                scene, local = "download", phase
            elif phase < 26:
                scene, local = "transcribe", phase - 10
            elif phase < 31:
                scene, local = "finishing", phase - 26
            else:
                scene, local = "idle", 0.0
        else:
            local = elapsed

        recent = [
            {
                "job_id": "demo-1", "kind": "process", "site": "youtube.com",
                "label": "9bZkp7q19f0", "finished_at": self._demo_started_at - 120,
                "ok": True,
            },
            {
                "job_id": "demo-2", "kind": "process", "site": "patreon.com",
                "label": "post-1234", "finished_at": self._demo_started_at - 900,
                "ok": True,
            },
            {
                "job_id": "demo-3", "kind": "discover", "site": "rss.example.com",
                "label": "", "finished_at": self._demo_started_at - 7200,
                "ok": False, "error": "Feed returned HTTP 404",
            },
        ]
        status: dict = {
            "last_heartbeat_at": now - 2,
            "last_heartbeat_ok": True,
            "recent_jobs": recent,
        }

        if scene == "download":
            pct = min(100.0, (local % 10.0) / 10.0 * 100.0)
            status.update({
                "state": "process",
                "site": "youtube.com",
                "stage": "downloading",
                "job_started_at": now - (local % 10.0),
                "progress": {
                    "stage": "downloading",
                    "percent": round(pct, 1),
                    "detail": f"{pct * 0.48:.1f} / 48.0 MB",
                },
            })
            return True, status

        if scene == "transcribe":
            pct = min(99.0, (local % 16.0) / 16.0 * 99.0)
            status.update({
                "state": "process",
                "site": "youtube.com",
                "stage": "transcribing",
                "job_started_at": now - (local % 16.0),
                "progress": {
                    "stage": "transcribing",
                    "percent": round(pct, 1),
                    "detail": f"segment {int(pct * 4) + 1}",
                },
            })
            return True, status

        if scene == "finishing":
            status.update({
                "state": "process",
                "site": "youtube.com",
                "stage": "finishing",
                "job_started_at": now - 30,
                "progress": {
                    "stage": "finishing",
                    "percent": 99,
                    "detail": "uploading transcript",
                },
            })
            return True, status

        if scene == "error":
            status.update({
                "state": "error",
                "site": "youtube.com",
                "error": "HTTP 401 Unauthorized: API key rejected by conductor",
            })
            return True, status

        if scene == "empty":
            status.update({"state": "stopped", "exit_reason": "empty_queue"})
            return False, status

        if scene == "stopped":
            status.update({"state": "stopped"})
            return False, status

        status.update({
            "state": "polling",
            "next_poll_at": now + (
                max(1.0, 38.0 - phase)
                if phase is not None
                else max(1.0, 5.0 - (local % 5.0))
            ),
        })
        return True, status

    def _maybe_quit_after_empty_queue(self, alive: bool, status: dict) -> None:
        if self._demo:
            return
        if (
            self._auto_quit_scheduled
            or not self._quit_when_idle
            or not self._worker_started
            or alive
            or self._pending_start
            or status.get("exit_reason") != "empty_queue"
        ):
            return
        self._auto_quit_scheduled = True
        if self._tray is not None:
            self._tray.showMessage(
                "No jobs available",
                "The queue is empty, so Blurb is exiting.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
        self._auto_quit_timer.start(1500)

    def _apply_status(self, alive: bool, status: dict) -> None:
        state = str(status.get("state") or "")
        stage = str(status.get("stage") or "")
        progress = status.get("progress") if isinstance(status.get("progress"), dict) else {}
        site = _shorten(str(status.get("site") or ""), 30)
        error = str(status.get("error") or "")
        restarting = time.time() < self._restarting_until and self._pending_start
        working = alive and state in ("discover", "process")
        exit_reason = ""
        if self._worker_started or self._auto_quit_scheduled:
            exit_reason = str(status.get("exit_reason") or "")

        self.server_lbl.setText(
            f"Server: {_shorten(_url_host(self._web_url), 28)}" if self._configured()
            else "Server: not configured"
        )

        if self._start_error:
            alive = False

        # Reset state-specific widgets.
        self.error_lbl.hide()
        self.error_hint_lbl.hide()
        self.progress_row_widget.hide()
        self.site_lbl.setText("")
        self.detail_lbl.setText("")
        self.next_lbl.setText("")
        self.conn_lbl.setText("")
        self.conn_dot.hide()

        if not self._configured():
            self.status_dot.set_color(COLOR_SUBTLE)
            self.status_lbl.setText("Not configured")
            self.detail_lbl.setText("Add a server URL and API key in Settings to get started.")
            self.detail_lbl.show()
            self._set_primary("Set up Blurb", "Primary", enabled=True)
        elif restarting:
            self.status_dot.set_color(COLOR_WARN)
            self.status_lbl.setText("Restarting…")
            self.detail_lbl.setText("Applying the new settings and bringing the worker back up.")
            self._set_primary("Restarting…", "Secondary", enabled=False)
        elif not alive:
            if self._start_error:
                self.status_dot.set_color(COLOR_STOP)
                self.status_lbl.setText("Could not start")
                self.error_lbl.setText(self._start_error)
                self.error_lbl.show()
                hint = _error_hint(self._start_error)
                if hint:
                    self.error_hint_lbl.setText(hint)
                    self.error_hint_lbl.show()
                self._set_primary("Start worker", "Primary", enabled=True)
            elif exit_reason == "empty_queue":
                self.status_dot.set_color(COLOR_SUBTLE)
                self.status_lbl.setText("No jobs available")
                if self._auto_quit_scheduled:
                    self.detail_lbl.setText("The queue was empty. Blurb is exiting…")
                    self._set_primary("Exiting…", "Secondary", enabled=False)
                else:
                    self.detail_lbl.setText("The queue was empty.")
                    self._set_primary("Start worker", "Primary", enabled=True)
                self.detail_lbl.show()
            else:
                self.status_dot.set_color(COLOR_SUBTLE)
                self.status_lbl.setText("Stopped")
                self.detail_lbl.setText("The worker is not running.")
                self.detail_lbl.show()
                self._set_primary("Start worker", "Primary", enabled=True)
        elif error or state == "error":
            self.status_dot.set_color(COLOR_STOP)
            self.status_lbl.setText("Error")
            if site:
                self.site_lbl.setText(site)
            self.error_lbl.setText(error or "The worker reported an unknown error.")
            self.error_lbl.show()
            hint = _error_hint(error)
            if hint:
                self.error_hint_lbl.setText(hint)
                self.error_hint_lbl.show()
            self._set_primary("Stop worker", "Danger", enabled=True)
        elif working:
            title = _status_title(state, stage, progress)
            self.status_dot.set_color(COLOR_WORK)
            self.status_lbl.setText(title)
            if site:
                self.site_lbl.setText(site)

            detail = str(progress.get("detail") or "").strip()
            parts = [p for p in (detail,) if p]
            started = status.get("job_started_at")
            elapsed = _format_duration(time.time() - started) if isinstance(started, (int, float)) else ""
            if elapsed:
                parts.append(elapsed)
            self.detail_lbl.setText(" · ".join(parts) if parts else "Working…")
            self.detail_lbl.show()

            pct = progress.get("percent")
            if isinstance(pct, (int, float)):
                value = max(0, min(100, int(round(pct))))
                self.progress.setRange(0, 100)
                self.progress.setValue(value)
                self.percent_lbl.setText(f"{value}%")
            else:
                self.progress.setRange(0, 0)
                self.percent_lbl.setText("…")
            self.progress_row_widget.show()
            self._set_primary("Stop worker", "Danger", enabled=True)
        else:
            # polling / starting / unknown
            if state == "polling":
                self.status_dot.set_color(COLOR_RUN)
                self.status_lbl.setText("Waiting for jobs")
                self.detail_lbl.setText("The worker is ready and checking the server for new work.")
            elif state == "stopped":
                self.status_dot.set_color(COLOR_SUBTLE)
                self.status_lbl.setText("Stopping…")
                self.detail_lbl.setText("The worker is shutting down.")
            else:
                self.status_dot.set_color(COLOR_WARN)
                self.status_lbl.setText("Starting…")
                self.detail_lbl.setText("Waiting for the worker to come online.")
            self.detail_lbl.show()
            self._set_primary("Stop worker", "Danger", enabled=True)

        # Connection line
        if alive and self._configured() and not restarting:
            hb_at = status.get("last_heartbeat_at")
            hb_ok = status.get("last_heartbeat_ok")
            self.conn_dot.show()
            if hb_at is None:
                self.conn_dot.set_color(COLOR_WARN)
                self.conn_lbl.setText("Connecting…")
                self.conn_lbl.setStyleSheet(f"color: {COLOR_MUTED};")
            else:
                stale = (time.time() - hb_at) > self._heartbeat_stale_after
                if hb_ok and not stale:
                    self.conn_dot.set_color(COLOR_RUN)
                    self.conn_lbl.setText(f"Connected · heartbeat {_relative_time(hb_at)}")
                    self.conn_lbl.setStyleSheet(f"color: {COLOR_MUTED};")
                else:
                    self.conn_dot.set_color(COLOR_STOP)
                    self.conn_lbl.setText("Connection issue")
                    self.conn_lbl.setStyleSheet(f"color: {COLOR_STOP};")

        if alive and state == "polling":
            next_poll_at = status.get("next_poll_at")
            if isinstance(next_poll_at, (int, float)):
                remaining = next_poll_at - time.time()
                if remaining > 0:
                    self.next_lbl.setText(f"Next check in {_format_countdown(remaining)}")
                else:
                    self.next_lbl.setText("Checking now…")

        self._update_window_title(
            alive, state, stage, progress, error, restarting, exit_reason,
        )
        self._update_tray(
            alive,
            self.windowTitle(),
            self._status_color(alive, state, working, error),
        )

    def _status_color(self, alive: bool, state: str, working: bool, error: str) -> str:
        if not alive:
            return COLOR_SUBTLE
        if error or state == "error":
            return COLOR_STOP
        if working:
            return COLOR_WORK
        if state == "polling":
            return COLOR_RUN
        return COLOR_WARN

    def _set_primary(self, text: str, style: str, enabled: bool) -> None:
        self.primary_btn.setText(text)
        self.primary_btn.setEnabled(enabled)
        if self.primary_btn.objectName() != style:
            self.primary_btn.setObjectName(style)
            self.primary_btn.style().unpolish(self.primary_btn)
            self.primary_btn.style().polish(self.primary_btn)

    def _update_window_title(
        self, alive: bool, state: str, stage: str, progress: dict, error: str,
        restarting: bool = False, exit_reason: str = "",
    ) -> None:
        if restarting:
            title = "Blurb — Restarting"
        elif not self._configured():
            title = "Blurb — Setup"
        elif not alive:
            if exit_reason == "empty_queue":
                title = "Blurb — No jobs"
            else:
                title = "Blurb — Stopped"
        elif error or state == "error":
            title = "Blurb — Error"
        elif state in ("discover", "process"):
            label = _status_title(state, stage, progress)
            pct = progress.get("percent")
            if isinstance(pct, (int, float)):
                title = f"Blurb — {label} {int(round(pct))}%"
            else:
                title = f"Blurb — {label}"
        elif state == "polling":
            title = "Blurb — Waiting for jobs"
        elif state == "stopped":
            title = "Blurb — Stopping"
        else:
            title = "Blurb — Starting"
        if title != self.windowTitle():
            self.setWindowTitle(title)

    # --------------------------------------------------------------- recent

    def _update_recent(self, jobs: list) -> None:
        jobs = [job for job in jobs if isinstance(job, dict)]
        try:
            signature = json.dumps(jobs, sort_keys=True, default=str)
        except Exception:
            signature = str(jobs)
        if signature != self._recent_signature:
            self._recent_signature = signature
            self._rebuild_recent(jobs)

        for label, timestamp in self._recent_time_labels:
            label.setText(_relative_time(timestamp))

    def _rebuild_recent(self, jobs: list) -> None:
        while self.recent_box.count():
            item = self.recent_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._recent_time_labels = []

        if not jobs:
            empty = QLabel("No recent jobs yet. Completed work will show up here.")
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            self.recent_box.addWidget(empty)
            return

        for job in jobs[:3]:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(9)

            ok = bool(job.get("ok"))
            icon = QLabel("✓" if ok else "✕")
            icon.setFixedWidth(16)
            icon.setStyleSheet(f"color: {COLOR_RUN if ok else COLOR_STOP}; font-weight: 600;")
            row_layout.addWidget(icon)

            kind = str(job.get("kind") or "")
            site = str(job.get("site") or "")
            label = str(job.get("label") or "")
            if ok:
                if kind == "discover":
                    title = f"Discovered {site or 'source'}"
                elif kind == "process":
                    title = f"Transcribed {site or 'audio'}"
                else:
                    title = site or "Job"
            else:
                verb = "discover" if kind == "discover" else "transcribe"
                title = f"Failed to {verb} {site or 'job'}"
            if label and label not in title:
                title = f"{title} · {label}"
            title = _shorten(title, 52)

            text = ElidedLabel(title)
            text.setObjectName("Muted")
            text.setToolTip(
                "\n".join(filter(None, [
                    f"Job: {job.get('job_id') or '—'}",
                    f"Finished: {_relative_time(job.get('finished_at'))}",
                    f"Error: {job.get('error')}" if job.get("error") else "",
                ]))
            )
            row_layout.addWidget(text, 1)

            finished_at = job.get("finished_at")
            if not isinstance(finished_at, (int, float)):
                finished_at = None
            time_lbl = QLabel(_relative_time(finished_at))
            time_lbl.setObjectName("Subtle")
            time_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
            row_layout.addWidget(time_lbl)
            if isinstance(finished_at, (int, float)):
                self._recent_time_labels.append((time_lbl, finished_at))

            self.recent_box.addWidget(row)

    # -------------------------------------------------------------- cookies

    def _refresh_cookie_labels(self) -> None:
        yt = _cookie_status("YOUTUBE_COOKIES_FILE")
        patreon = _cookie_status("PATREON_COOKIES_FILE")

        yt_text, yt_color = _cookie_words(yt)
        self.yt_cookie_status.setText(yt_text)
        self.yt_cookie_status.setStyleSheet(f"color: {yt_color};")

        patreon_text, patreon_color = _cookie_words(patreon)
        self.patreon_cookie_status.setText(patreon_text)
        self.patreon_cookie_status.setStyleSheet(f"color: {patreon_color};")

        issues = [issue for issue in (
            _cookie_issue("YouTube", yt),
            _cookie_issue("Patreon", patreon),
        ) if issue]
        if issues:
            self.warning_lbl.setText(" ".join(issues))
            self.warning.show()
        else:
            self.warning.hide()

    # ----------------------------------------------------------------- logs

    def _show_logs(self) -> None:
        if self._log_dialog is not None:
            try:
                self._log_dialog.raise_()
                self._log_dialog.activateWindow()
                return
            except RuntimeError:
                self._log_dialog = None
        dialog = LogViewer(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.destroyed.connect(lambda: setattr(self, "_log_dialog", None))
        self._log_dialog = dialog
        dialog.show()

    # ------------------------------------------------------------ close/quit

    def closeEvent(self, event) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())
        if self._tray is not None and self._tray.isVisible():
            event.ignore()
            self.hide()
            if not self._settings.value("ui/close_hint_shown", False, type=bool):
                self._settings.setValue("ui/close_hint_shown", True)
                self._tray.showMessage(
                    "Blurb is still running",
                    "The worker keeps running in the system tray. "
                    "Use the tray menu to reopen or quit.",
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )
            return
        self._quit_app()

    def _quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._auto_quit_timer.stop()
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._stop_worker()
        if self._tray is not None:
            self._tray.hide()
        QApplication.quit()


def _dark_palette() -> QPalette:
    """Dark defaults for native widgets and dialogs that QSS doesn't reach."""
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: COLOR_BG,
        QPalette.ColorRole.WindowText: COLOR_FG,
        QPalette.ColorRole.Base: COLOR_CARD_DARK,
        QPalette.ColorRole.AlternateBase: COLOR_CARD,
        QPalette.ColorRole.Text: COLOR_FG,
        QPalette.ColorRole.Button: COLOR_CARD,
        QPalette.ColorRole.ButtonText: COLOR_FG,
        QPalette.ColorRole.BrightText: COLOR_STOP,
        QPalette.ColorRole.Highlight: COLOR_ACCENT,
        QPalette.ColorRole.HighlightedText: COLOR_BTN_FG,
        QPalette.ColorRole.ToolTipBase: COLOR_CARD,
        QPalette.ColorRole.ToolTipText: COLOR_FG,
        QPalette.ColorRole.PlaceholderText: COLOR_SUBTLE,
        QPalette.ColorRole.Mid: COLOR_BORDER,
        QPalette.ColorRole.Dark: COLOR_BG,
        QPalette.ColorRole.Light: COLOR_BORDER,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    return palette


def run(demo: bool = False) -> None:
    """Create the application and show the manager window."""
    if demo:
        demo_dir = Path(tempfile.mkdtemp(prefix="blurb-demo-"))
        os.environ["BLURB_CONFIG_DIR"] = str(demo_dir)
        os.environ["XDG_CONFIG_HOME"] = str(demo_dir / "xdg-config")
        (demo_dir / ".env").write_text(
            "WEB_URL=https://demo.invalid/api\n"
            "BLURB_API_KEY=demo-key\n"
            "BLURB_AUTO_START_WORKER=false\n"
            "BLURB_QUIT_WHEN_IDLE=false\n"
            "HEARTBEAT_INTERVAL=60\n"
            "WHISPER_MODEL=distil-large-v3\n"
            "WHISPER_DEVICE=auto\n"
        )
        atexit.register(shutil.rmtree, demo_dir, ignore_errors=True)
        sys.argv = [arg for arg in sys.argv if arg != "--demo"]

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Blurb")
    app.setApplicationDisplayName("Blurb")
    app.setOrganizationName("Blurb")
    app.setDesktopFileName("blurb")
    app.setQuitOnLastWindowClosed(False)
    app.setPalette(_dark_palette())
    icon_path = str(paths.resource_path("blurb.png"))
    if Path(icon_path).is_file():
        app.setWindowIcon(QIcon(icon_path))

    qss = _APP_QSS
    check_icon = _ensure_check_icon()
    if check_icon:
        qss += f'\nQCheckBox::indicator:checked {{ image: url("{check_icon}"); }}\n'
    arrow_icon = _ensure_arrow_icon()
    if arrow_icon:
        qss += (
            f'\nQComboBox::down-arrow {{ image: url("{arrow_icon}"); '
            'width: 10px; height: 6px; }\n'
        )
    app.setStyleSheet(qss)
    manager = BlurbManager(demo=demo)
    manager.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run(demo="--demo" in sys.argv[1:])
