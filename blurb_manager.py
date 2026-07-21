"""
Blurb Manager — control panel for the Blurb pull-worker.

Manages web_worker.py as a background process and shows live status.
Auto-started on login via ~/.config/autostart/blurb-manager.desktop.
"""

import json
import os
import signal
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor
from PyQt6.QtCore import QTimer, Qt

BLURB_DIR   = Path(__file__).parent
VENV_PYTHON = BLURB_DIR / "venv-linux" / "bin" / "python"
STATUS_FILE = Path("/tmp/blurb_worker_status.json")
POLL_MS     = 2000

_BASE_PAD   = dict(padx=14, pady=6)
FONT_LABEL  = ("Sans", 10)
FONT_STATUS = ("Sans", 11, "bold")
FONT_SMALL  = ("Sans", 9)
COLOR_RUN       = "#2ecc71"
COLOR_WORK      = "#f39c12"
COLOR_STOP      = "#e74c3c"
COLOR_BG        = "#1e1e2e"
COLOR_FG        = "#cdd6f4"
COLOR_CARD      = "#313244"
COLOR_BTN_START = "#a6e3a1"
COLOR_BTN_STOP  = "#f38ba8"
COLOR_BTN_FG    = "#1e1e2e"
COLOR_MUTED     = "#6c7086"

# Human labels for the job kinds web_worker.py reports as `state`.
_STATE_LABELS = {"discover": "Discovering", "process": "Transcribing"}

# Flag a cookie file as "rotate soon" once it's within this many days of its
# earliest-expiring persistent cookie.
_COOKIE_ROTATE_WARNING_DAYS = 7


def _detect_ui_scale() -> float:
    """Best-effort desktop UI scale factor (1.0 == standard 96dpi).

    Tk's own DPI auto-detection uses the X11 core protocol's mm-based screen
    size, which desktops generally leave at the monitor's raw EDID value even
    when the user has configured HiDPI scaling via Xft.dpi/GDK_SCALE/
    QT_SCALE_FACTOR — the settings GTK/Qt/fontconfig apps actually honor. On
    such a desktop Tk renders at ~1x while everything else renders scaled, so
    check those sources directly instead of trusting Tk's default.
    """
    for var in ("GDK_SCALE", "QT_SCALE_FACTOR"):
        val = os.environ.get(var)
        if val:
            try:
                return max(1.0, float(val))
            except ValueError:
                pass
    try:
        out = subprocess.run(["xrdb", "-query"], capture_output=True, text=True, timeout=1)
        for line in out.stdout.splitlines():
            if line.startswith("Xft.dpi"):
                return max(1.0, float(line.split(":")[1].strip()) / 96.0)
    except Exception:
        pass
    return 1.0


def _read_env_var(key: str) -> str:
    env_path = BLURB_DIR / ".env"
    if not env_path.exists():
        return ""
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{key}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()
    return ""


def _write_env_vars(updates: dict) -> None:
    """Update (or append) key=value lines in .env, preserving everything else.

    Falls back to .env.example as a starting point if .env doesn't exist yet
    (matches what install.sh does on first setup).
    """
    env_path = BLURB_DIR / ".env"
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    else:
        example = BLURB_DIR / ".env.example"
        lines = example.read_text().splitlines() if example.exists() else []

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

    env_path.write_text("\n".join(lines) + "\n")


def _read_worker_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
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
             "expires_at": float | None}
    """
    path_str = _read_env_var(env_key)
    if not path_str:
        return {"state": "unset", "expires_at": None}

    path = Path(path_str).expanduser()
    if not path.exists():
        return {"state": "missing", "expires_at": None}

    expiry = _parse_cookie_expiry(path)
    if expiry is None:
        return {"state": "unknown", "expires_at": None}

    now = time.time()
    if expiry <= now:
        return {"state": "expired", "expires_at": expiry}
    if expiry <= now + _COOKIE_ROTATE_WARNING_DAYS * 86400:
        return {"state": "expiring", "expires_at": expiry}
    return {"state": "ok", "expires_at": expiry}


def _format_cookie_status(label: str, status: dict) -> tuple[str, str]:
    state      = status["state"]
    expires_at = status.get("expires_at")
    date_str   = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d") if expires_at else ""

    if state == "unset":
        return f"{label}:  not configured", COLOR_MUTED
    if state == "missing":
        return f"{label}:  file missing", COLOR_STOP
    if state == "unknown":
        return f"{label}:  configured", COLOR_RUN
    if state == "expired":
        return f"{label}:  expired {date_str} — rotate now", COLOR_STOP
    if state == "expiring":
        return f"{label}:  expires {date_str} — rotate soon", COLOR_WORK
    return f"{label}:  valid until {date_str}", COLOR_RUN


# ============================================================================
# Qt system-tray thread
# ============================================================================

class _QtTrayThread(threading.Thread):
    """Runs a minimal Qt event loop for the system tray icon."""

    def __init__(self, alive: bool, on_show, on_toggle, on_quit):
        super().__init__(daemon=True)
        self._on_show    = on_show
        self._on_toggle  = on_toggle
        self._on_quit    = on_quit
        self._alive      = alive
        self._working    = False
        self._title      = "Blurb"
        self._dirty      = True
        self._lock       = threading.Lock()
        self._qt_app: QApplication | None = None

    def set_state(self, alive: bool, working: bool, title: str):
        with self._lock:
            if self._alive != alive or self._working != working or self._title != title:
                self._alive   = alive
                self._working = working
                self._title   = title
                self._dirty   = True

    def quit(self):
        if self._qt_app is not None:
            self._qt_app.quit()

    def run(self):
        self._qt_app = QApplication([])
        self._tray   = QSystemTrayIcon()
        self._tray.activated.connect(self._handle_activate)

        menu = QMenu()
        menu.addAction("Show").triggered.connect(self._on_show)
        self._toggle_act = menu.addAction("Start Worker")
        self._toggle_act.triggered.connect(self._on_toggle)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self._on_quit)
        self._tray.setContextMenu(menu)

        self._refresh()
        self._tray.show()

        timer = QTimer()
        timer.timeout.connect(self._poll)
        timer.start(500)

        self._qt_app.exec()
        self._tray.hide()

    def _handle_activate(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._on_show()

    def _poll(self):
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
        self._refresh()

    def _refresh(self):
        with self._lock:
            alive   = self._alive
            working = self._working
            title   = self._title

        if not alive:
            color = QColor(COLOR_STOP)
        elif working:
            color = QColor(COLOR_WORK)
        else:
            color = QColor(COLOR_RUN)

        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(4, 4, 56, 56)
        painter.end()

        self._tray.setIcon(QIcon(pixmap))
        self._tray.setToolTip(title)
        self._toggle_act.setText("Stop Worker" if alive else "Start Worker")


# ============================================================================
# Manager
# ============================================================================

class BlurbManager:
    def __init__(self, root: tk.Tk):
        self.root        = root
        self.worker_pid: int | None        = None
        self._tray_thread: _QtTrayThread | None = None
        self._web_url    = _read_env_var("WEB_URL")
        self._api_key    = _read_env_var("BLURB_API_KEY")
        hb_interval      = int(_read_env_var("HEARTBEAT_INTERVAL") or 60)
        self._heartbeat_stale_after = hb_interval * 3
        self._key_visible = False

        self.scale = _detect_ui_scale()
        self.pad   = {k: self._px(v) for k, v in _BASE_PAD.items()}

        self._setup_window()
        self._build_ui()

        if self._configured():
            self._start_worker()
        else:
            self._show_settings()
        self._schedule_poll()

    def _configured(self) -> bool:
        return bool(self._web_url and self._api_key)

    def _px(self, n: int) -> int:
        """Scale a literal pixel value to match Tk's detected display scale."""
        return round(n * self.scale)

    # ------------------------------------------------------------------ setup

    def _setup_window(self):
        # Override Tk's own (mm-based, often desktop-scale-blind) DPI guess so
        # point-sized fonts render at the same scale as our pixel geometry.
        self.root.tk.call('tk', 'scaling', (96 / 72) * self.scale)
        self.root.title("Blurb")
        self.root.geometry(f"{self._px(320)}x{self._px(320)}")
        self.root.minsize(self._px(260), self._px(260))
        self.root.resizable(True, True)
        self.root.configure(bg=COLOR_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            icon = tk.PhotoImage(file=str(BLURB_DIR / "blurb.png"))
            self.root.iconphoto(True, icon)
        except Exception:
            pass

    def _build_ui(self):
        self.dashboard_frame = self._build_dashboard()
        self.settings_frame  = self._build_settings()
        self._refresh_static_labels()
        self.dashboard_frame.pack(fill=tk.BOTH, expand=True)

    def _build_dashboard(self) -> tk.Frame:
        f = tk.Frame(self.root, bg=COLOR_BG)

        top = tk.Frame(f, bg=COLOR_BG)
        top.pack(fill=tk.X, **self.pad)

        self.dot = tk.Label(top, text="●", font=("Sans", 16), fg=COLOR_STOP, bg=COLOR_BG)
        self.dot.pack(side=tk.LEFT)

        self.status_lbl = tk.Label(top, text="Starting…", font=FONT_STATUS,
                                   fg=COLOR_FG, bg=COLOR_BG)
        self.status_lbl.pack(side=tk.LEFT, padx=(6, 0))

        gear = tk.Button(top, text="⚙ Settings", font=FONT_SMALL,
                         fg=COLOR_MUTED, bg=COLOR_BG, relief=tk.FLAT, bd=0,
                         cursor="hand2", activebackground=COLOR_BG,
                         activeforeground=COLOR_FG, command=self._show_settings)
        gear.pack(side=tk.RIGHT)

        card = tk.Frame(f, bg=COLOR_CARD, bd=0)
        card.pack(fill=tk.X, padx=14, pady=(0, 8))

        self.job_lbl = tk.Label(card, text="Current job:  —",
                                anchor="w", font=FONT_LABEL,
                                fg=COLOR_FG, bg=COLOR_CARD)
        self.job_lbl.pack(fill=tk.X, padx=10, pady=(6, 2))

        self.conn_lbl = tk.Label(card, text="", anchor="w", font=FONT_LABEL,
                                 fg=COLOR_MUTED, bg=COLOR_CARD)
        self.conn_lbl.pack(fill=tk.X, padx=10, pady=2)

        self.url_lbl = tk.Label(card, text="", anchor="w", font=FONT_LABEL,
                                fg=COLOR_MUTED, bg=COLOR_CARD)
        self.url_lbl.pack(fill=tk.X, padx=10, pady=(2, 2))

        self.yt_cookie_lbl = tk.Label(card, text="", anchor="w", font=FONT_SMALL,
                                      fg=COLOR_MUTED, bg=COLOR_CARD)
        self.yt_cookie_lbl.pack(fill=tk.X, padx=10, pady=1)

        self.patreon_cookie_lbl = tk.Label(card, text="", anchor="w", font=FONT_SMALL,
                                           fg=COLOR_MUTED, bg=COLOR_CARD)
        self.patreon_cookie_lbl.pack(fill=tk.X, padx=10, pady=(1, 6))

        self.btn = tk.Button(f, text="Stop", width=12,
                             font=("Sans", 10, "bold"),
                             fg=COLOR_BTN_FG, bg=COLOR_BTN_STOP,
                             relief=tk.FLAT, cursor="hand2",
                             command=self._toggle_worker)
        self.btn.pack(pady=(0, 10))

        return f

    def _build_settings(self) -> tk.Frame:
        f = tk.Frame(self.root, bg=COLOR_BG)

        top = tk.Frame(f, bg=COLOR_BG)
        top.pack(fill=tk.X, **self.pad)

        back = tk.Button(top, text="← Back", font=FONT_LABEL,
                         fg=COLOR_MUTED, bg=COLOR_BG, relief=tk.FLAT, bd=0,
                         cursor="hand2", activebackground=COLOR_BG,
                         activeforeground=COLOR_FG, command=self._show_dashboard)
        back.pack(side=tk.LEFT)

        tk.Label(top, text="Settings", font=FONT_STATUS, fg=COLOR_FG,
                bg=COLOR_BG).pack(side=tk.LEFT, padx=(10, 0))

        body = tk.Frame(f, bg=COLOR_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=14)

        tk.Label(body, text="Remote URL", anchor="w", font=FONT_LABEL,
                fg=COLOR_MUTED, bg=COLOR_BG).pack(fill=tk.X, pady=(4, 0))
        self.url_var = tk.StringVar()
        url_entry = tk.Entry(body, textvariable=self.url_var, font=FONT_LABEL,
                             bg=COLOR_CARD, fg=COLOR_FG, insertbackground=COLOR_FG,
                             relief=tk.FLAT)
        url_entry.pack(fill=tk.X, ipady=self._px(4), pady=(2, 8))

        tk.Label(body, text="API Key", anchor="w", font=FONT_LABEL,
                fg=COLOR_MUTED, bg=COLOR_BG).pack(fill=tk.X)
        key_row = tk.Frame(body, bg=COLOR_BG)
        key_row.pack(fill=tk.X, pady=(2, 4))
        self.key_var = tk.StringVar()
        self.key_entry = tk.Entry(key_row, textvariable=self.key_var, font=FONT_LABEL,
                                  show="•", bg=COLOR_CARD, fg=COLOR_FG,
                                  insertbackground=COLOR_FG, relief=tk.FLAT)
        self.key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=self._px(4))
        self.show_btn = tk.Button(key_row, text="Show", font=FONT_SMALL, width=4,
                                  fg=COLOR_BTN_FG, bg=COLOR_CARD, relief=tk.FLAT,
                                  cursor="hand2", command=self._toggle_key_visibility)
        self.show_btn.pack(side=tk.LEFT, padx=(4, 0))

        self.settings_error_lbl = tk.Label(body, text="", anchor="w", font=FONT_SMALL,
                                           fg=COLOR_STOP, bg=COLOR_BG,
                                           wraplength=self._px(280), justify=tk.LEFT)
        self.settings_error_lbl.pack(fill=tk.X, pady=(2, 4))

        save_btn = tk.Button(body, text="Save", font=("Sans", 10, "bold"),
                             fg=COLOR_BTN_FG, bg=COLOR_BTN_START, relief=tk.FLAT,
                             cursor="hand2", command=self._save_settings)
        save_btn.pack(pady=(4, 10))

        return f

    def _refresh_static_labels(self):
        display_url = self._web_url or "not configured"
        if len(display_url) > 32:
            display_url = display_url[:29] + "…"
        self.url_lbl.config(text=f"Remote:  {display_url}")

    # ---------------------------------------------------------------- panels

    def _show_settings(self):
        self.url_var.set(self._web_url)
        self.key_var.set(self._api_key)
        self._key_visible = False
        self.key_entry.config(show="•")
        self.show_btn.config(text="Show")
        self.settings_error_lbl.config(text="")
        self.dashboard_frame.pack_forget()
        self.settings_frame.pack(fill=tk.BOTH, expand=True)

    def _show_dashboard(self):
        self.settings_frame.pack_forget()
        self.dashboard_frame.pack(fill=tk.BOTH, expand=True)

    def _toggle_key_visibility(self):
        self._key_visible = not self._key_visible
        self.key_entry.config(show="" if self._key_visible else "•")
        self.show_btn.config(text="Hide" if self._key_visible else "Show")

    def _save_settings(self):
        url = self.url_var.get().strip()
        key = self.key_var.get().strip()
        if not url or not key:
            self.settings_error_lbl.config(text="Both fields are required.")
            return

        _write_env_vars({"WEB_URL": url, "BLURB_API_KEY": key})
        was_alive     = self._is_worker_alive()
        self._web_url = url
        self._api_key = key
        self._refresh_static_labels()
        self._show_dashboard()

        if was_alive:
            # New env vars only take effect for a freshly-started process.
            self._stop_worker()
            self.root.after(500, self._start_worker)
        else:
            self._start_worker()

    # -------------------------------------------------------- process control

    def _start_worker(self):
        if not self._configured() or self._is_worker_alive():
            return
        proc = subprocess.Popen(
            [str(VENV_PYTHON), str(BLURB_DIR / "web_worker.py")],
            cwd=str(BLURB_DIR),
        )
        self.worker_pid = proc.pid

    def _stop_worker(self):
        if self.worker_pid is not None:
            try:
                os.kill(self.worker_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.worker_pid = None

    def _toggle_worker(self):
        if self._is_worker_alive():
            self._stop_worker()
        else:
            self._start_worker()

    def _is_worker_alive(self) -> bool:
        if self.worker_pid is None:
            return False
        try:
            os.kill(self.worker_pid, 0)
            return True
        except ProcessLookupError:
            self.worker_pid = None
            return False

    # --------------------------------------------------------- system tray

    def _on_close(self):
        self.root.withdraw()
        if self._tray_thread is None:
            self._create_tray()

    def _create_tray(self):
        self._tray_thread = _QtTrayThread(
            alive=self._is_worker_alive(),
            on_show=lambda: self.root.after(0, self._restore_window),
            on_toggle=lambda: self.root.after(0, self._toggle_worker),
            on_quit=lambda: self.root.after(0, self._quit_app),
        )
        self._tray_thread.start()

    def _restore_window(self):
        self.root.deiconify()
        self.root.lift()
        if self._tray_thread is not None:
            self._tray_thread.quit()
            self._tray_thread = None

    def _quit_app(self):
        self._stop_worker()
        if self._tray_thread is not None:
            self._tray_thread.quit()
            self._tray_thread = None
        self.root.destroy()

    # -------------------------------------------------------------- polling

    def _schedule_poll(self):
        threading.Thread(target=self._fetch_and_update, daemon=True).start()

    def _fetch_and_update(self):
        alive         = self._is_worker_alive()
        status        = _read_worker_status() if alive else {}
        yt_status     = _cookie_status("YOUTUBE_COOKIES_FILE")
        patreon_status = _cookie_status("PATREON_COOKIES_FILE")
        self.root.after(0, lambda: self._apply_and_reschedule(alive, status, yt_status, patreon_status))

    def _apply_and_reschedule(self, alive, status, yt_status, patreon_status):
        self._update_ui(alive, status)
        self._update_tray(alive, status)
        self._update_cookie_labels(yt_status, patreon_status)
        self.root.after(POLL_MS, self._schedule_poll)

    def _update_cookie_labels(self, yt_status: dict, patreon_status: dict):
        text, color = _format_cookie_status("YouTube", yt_status)
        self.yt_cookie_lbl.config(text=text, fg=color)
        text, color = _format_cookie_status("Patreon", patreon_status)
        self.patreon_cookie_lbl.config(text=text, fg=color)

    def _update_tray(self, alive: bool, status: dict):
        if self._tray_thread is None:
            return
        state   = status.get("state", "")
        working = state in ("discover", "process")
        if alive and working:
            title = f"Blurb — {_STATE_LABELS.get(state, state.title())}"
        elif alive and state == "error":
            title = "Blurb — error"
        elif alive:
            title = "Blurb — running"
        else:
            title = "Blurb — stopped"
        self._tray_thread.set_state(alive, working, title)

    def _connection_text_and_color(self, hb_at, hb_ok) -> tuple[str, str]:
        if hb_at is None:
            return "Connecting…", COLOR_MUTED
        stale = (time.time() - hb_at) > self._heartbeat_stale_after
        if hb_ok and not stale:
            return "Connected", COLOR_RUN
        return "Connection issue", COLOR_STOP

    def _update_ui(self, alive: bool, status: dict):
        state = status.get("state", "")
        job_id = status.get("job_id")
        error  = status.get("error")
        site   = status.get("site")
        working = state in ("discover", "process")

        if not alive:
            self.dot.config(fg=COLOR_STOP)
            self.status_lbl.config(text="Stopped")
            self.btn.config(text="Start", bg=COLOR_BTN_START)
            self.job_lbl.config(text="Current job:  —")
            self.conn_lbl.config(text="")
        elif working:
            label = _STATE_LABELS.get(state, state.title())
            self.dot.config(fg=COLOR_WORK)
            self.status_lbl.config(text=label)
            self.btn.config(text="Stop", bg=COLOR_BTN_STOP)
            detail = f"{label}:  {site or job_id or '—'}"
            self.job_lbl.config(text=detail[:40])
        elif state == "error":
            self.dot.config(fg=COLOR_STOP)
            self.status_lbl.config(text="Error")
            self.btn.config(text="Stop", bg=COLOR_BTN_STOP)
            self.job_lbl.config(text=f"Error:  {(error or 'unknown')[:28]}")
        else:
            # polling / starting / unknown
            self.dot.config(fg=COLOR_RUN)
            self.status_lbl.config(text="Polling" if state == "polling" else "Starting…")
            self.btn.config(text="Stop", bg=COLOR_BTN_STOP)
            next_poll_at = status.get("next_poll_at")
            if state == "polling" and next_poll_at:
                next_str = datetime.fromtimestamp(next_poll_at).strftime("%H:%M:%S")
                self.job_lbl.config(text=f"Next poll:  {next_str}")
            else:
                self.job_lbl.config(text="Current job:  idle")

        if alive:
            text, color = self._connection_text_and_color(
                status.get("last_heartbeat_at"), status.get("last_heartbeat_ok")
            )
            self.conn_lbl.config(text=text, fg=color)
        else:
            self.conn_lbl.config(text="")

        if not self._configured():
            self.btn.config(state=tk.DISABLED, text="Not configured")
        else:
            self.btn.config(state=tk.NORMAL)


if __name__ == "__main__":
    root = tk.Tk()
    BlurbManager(root)
    root.mainloop()
