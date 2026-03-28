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

PAD         = dict(padx=14, pady=6)
FONT_LABEL  = ("Sans", 10)
FONT_STATUS = ("Sans", 11, "bold")
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


def _read_worker_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


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
        self._setup_window()
        self._build_ui()
        if self._web_url:
            self._start_worker()
        self._schedule_poll()

    # ------------------------------------------------------------------ setup

    def _setup_window(self):
        self.root.title("Blurb")
        self.root.geometry("300x190")
        self.root.resizable(False, False)
        self.root.configure(bg=COLOR_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            icon = tk.PhotoImage(file=str(BLURB_DIR / "blurb.png"))
            self.root.iconphoto(True, icon)
        except Exception:
            pass

    def _build_ui(self):
        top = tk.Frame(self.root, bg=COLOR_BG)
        top.pack(fill=tk.X, **PAD)

        self.dot = tk.Label(top, text="●", font=("Sans", 16), fg=COLOR_STOP, bg=COLOR_BG)
        self.dot.pack(side=tk.LEFT)

        self.status_lbl = tk.Label(top, text="Starting…", font=FONT_STATUS,
                                   fg=COLOR_FG, bg=COLOR_BG)
        self.status_lbl.pack(side=tk.LEFT, padx=(6, 0))

        card = tk.Frame(self.root, bg=COLOR_CARD, bd=0)
        card.pack(fill=tk.X, padx=14, pady=(0, 8))

        self.job_lbl = tk.Label(card, text="Current job:  —",
                                anchor="w", font=FONT_LABEL,
                                fg=COLOR_FG, bg=COLOR_CARD)
        self.job_lbl.pack(fill=tk.X, padx=10, pady=(6, 2))

        display_url = self._web_url or "not configured"
        if len(display_url) > 32:
            display_url = display_url[:29] + "…"
        self.url_lbl = tk.Label(card, text=f"Remote:  {display_url}",
                                anchor="w", font=FONT_LABEL,
                                fg=COLOR_MUTED, bg=COLOR_CARD)
        self.url_lbl.pack(fill=tk.X, padx=10, pady=(2, 6))

        self.btn = tk.Button(self.root, text="Stop", width=12,
                             font=("Sans", 10, "bold"),
                             fg=COLOR_BTN_FG, bg=COLOR_BTN_STOP,
                             relief=tk.FLAT, cursor="hand2",
                             command=self._toggle_worker)
        self.btn.pack(pady=(0, 10))

        if not self._web_url:
            self.btn.config(state=tk.DISABLED, text="Not configured")

    # -------------------------------------------------------- process control

    def _start_worker(self):
        if self._is_worker_alive():
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
        alive  = self._is_worker_alive()
        status = _read_worker_status() if alive else {}
        self.root.after(0, lambda: self._apply_and_reschedule(alive, status))

    def _apply_and_reschedule(self, alive, status):
        self._update_ui(alive, status)
        self._update_tray(alive, status)
        self.root.after(POLL_MS, self._schedule_poll)

    def _update_tray(self, alive: bool, status: dict):
        if self._tray_thread is None:
            return
        state   = status.get("state", "")
        working = state == "transcribing"
        title   = f"Blurb — {state}" if (alive and state) else ("Blurb — running" if alive else "Blurb — stopped")
        self._tray_thread.set_state(alive, working, title)

    def _update_ui(self, alive: bool, status: dict):
        state  = status.get("state", "")
        job_id = status.get("job_id")
        error  = status.get("error")

        if not alive:
            self.dot.config(fg=COLOR_STOP)
            self.status_lbl.config(text="Stopped")
            self.btn.config(text="Start", bg=COLOR_BTN_START)
            self.job_lbl.config(text="Current job:  —")
        elif state == "transcribing":
            self.dot.config(fg=COLOR_WORK)
            self.status_lbl.config(text="Transcribing")
            self.btn.config(text="Stop", bg=COLOR_BTN_STOP)
            self.job_lbl.config(text=f"Current job:  {job_id or '—'}")
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

        if not self._web_url:
            self.btn.config(state=tk.DISABLED, text="Not configured")


if __name__ == "__main__":
    root = tk.Tk()
    BlurbManager(root)
    root.mainloop()
