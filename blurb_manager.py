"""
Blurb Manager — small control panel for the blurb transcription service.

On launch, attaches to an existing blurb process if one is running, otherwise
starts a new one. Only one blurb process is allowed at a time.
Auto-started on login via ~/.config/autostart/blurb-manager.desktop.
"""

import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor
from PyQt6.QtCore import QTimer, Qt

BLURB_DIR = Path(__file__).parent
VENV_PYTHON = BLURB_DIR / "venv-linux" / "bin" / "python"
BLURB_PORT = 8001
STATUS_URL = f"http://localhost:{BLURB_PORT}/status"
POLL_MS = 2000

PAD = dict(padx=14, pady=6)
FONT_LABEL = ("Sans", 10)
FONT_STATUS = ("Sans", 11, "bold")
COLOR_RUN = "#2ecc71"
COLOR_STOP = "#e74c3c"
COLOR_BG = "#1e1e2e"
COLOR_FG = "#cdd6f4"
COLOR_CARD = "#313244"
COLOR_BTN_START = "#a6e3a1"
COLOR_BTN_STOP = "#f38ba8"
COLOR_BTN_FG = "#1e1e2e"


# ============================================================================
# Qt system-tray thread
# ============================================================================

class _QtTrayThread(threading.Thread):
    """Runs a minimal Qt event loop for the system tray icon."""

    def __init__(self, alive: bool, on_show, on_toggle, on_quit):
        super().__init__(daemon=True)
        self._on_show = on_show
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        self._alive = alive
        self._title = "Blurb"
        self._dirty = True
        self._lock = threading.Lock()
        self._qt_app: QApplication | None = None

    def set_state(self, alive: bool, title: str):
        with self._lock:
            if self._alive != alive or self._title != title:
                self._alive = alive
                self._title = title
                self._dirty = True

    def quit(self):
        if self._qt_app is not None:
            self._qt_app.quit()

    def run(self):
        self._qt_app = QApplication([])

        self._tray = QSystemTrayIcon()
        self._tray.activated.connect(self._handle_activate)

        menu = QMenu()
        menu.addAction("Show").triggered.connect(self._on_show)
        self._toggle_act = menu.addAction("Start Blurb")
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
            alive = self._alive
            title = self._title

        color = QColor(COLOR_RUN if alive else COLOR_STOP)
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
        self._toggle_act.setText("Stop Blurb" if alive else "Start Blurb")


# ============================================================================
# Helper
# ============================================================================

def _find_existing_pid() -> int | None:
    """Return PID of any process currently listening on BLURB_PORT, or None."""
    try:
        result = subprocess.run(
            ["fuser", f"{BLURB_PORT}/tcp"],
            capture_output=True, text=True
        )
        pids = result.stdout.split()
        if pids:
            return int(pids[0])
    except Exception:
        pass
    return None


class BlurbManager:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.pid: int | None = None
        self._conn: http.client.HTTPConnection | None = None
        self._tray_thread: _QtTrayThread | None = None
        self._last_alive = False
        self._setup_window()
        self._build_ui()
        self._attach_or_start()
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
        # --- status row ---
        top = tk.Frame(self.root, bg=COLOR_BG)
        top.pack(fill=tk.X, **PAD)

        self.dot = tk.Label(top, text="●", font=("Sans", 16), fg=COLOR_STOP, bg=COLOR_BG)
        self.dot.pack(side=tk.LEFT)

        self.status_lbl = tk.Label(top, text="Starting…", font=FONT_STATUS,
                                   fg=COLOR_FG, bg=COLOR_BG)
        self.status_lbl.pack(side=tk.LEFT, padx=(6, 0))

        # --- stats card ---
        card = tk.Frame(self.root, bg=COLOR_CARD, bd=0)
        card.pack(fill=tk.X, padx=14, pady=(0, 8))

        self.job_lbl = tk.Label(card, text="Active job:       —",
                                anchor="w", font=FONT_LABEL,
                                fg=COLOR_FG, bg=COLOR_CARD)
        self.job_lbl.pack(fill=tk.X, padx=10, pady=(6, 2))

        self.queue_lbl = tk.Label(card, text="Jobs in memory:  —",
                                  anchor="w", font=FONT_LABEL,
                                  fg=COLOR_FG, bg=COLOR_CARD)
        self.queue_lbl.pack(fill=tk.X, padx=10, pady=(2, 6))

        # --- button ---
        self.btn = tk.Button(self.root, text="Stop", width=12,
                             font=("Sans", 10, "bold"),
                             fg=COLOR_BTN_FG, bg=COLOR_BTN_STOP,
                             relief=tk.FLAT, cursor="hand2",
                             command=self._toggle)
        self.btn.pack(pady=(0, 10))

    # -------------------------------------------------------- process control

    def _attach_or_start(self):
        """Attach to an existing blurb process, or start a new one."""
        existing = _find_existing_pid()
        if existing:
            self.pid = existing
        else:
            self._start_blurb()

    def _start_blurb(self):
        proc = subprocess.Popen(
            [str(VENV_PYTHON), "-m", "uvicorn", "main:app",
             "--host", "0.0.0.0", "--port", str(BLURB_PORT)],
            cwd=str(BLURB_DIR),
        )
        self.pid = proc.pid

    def _stop_blurb(self):
        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.pid = None

    def _toggle(self):
        if self._is_process_alive():
            self._stop_blurb()
        else:
            self._start_blurb()

    def _is_process_alive(self) -> bool:
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)  # signal 0 = existence check, no actual signal sent
            return True
        except ProcessLookupError:
            self.pid = None
            return False

    def _on_close(self):
        """Minimize to system tray instead of closing."""
        self.root.withdraw()
        if self._tray_thread is None:
            self._create_tray()

    # --------------------------------------------------------- system tray

    def _create_tray(self):
        self._tray_thread = _QtTrayThread(
            alive=self._last_alive,
            on_show=lambda: self.root.after(0, self._restore_window),
            on_toggle=lambda: self.root.after(0, self._toggle),
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
        if self._tray_thread is not None:
            self._tray_thread.quit()
            self._tray_thread = None
        self.root.destroy()

    # -------------------------------------------------------------- polling

    def _schedule_poll(self):
        threading.Thread(target=self._fetch_and_update, daemon=True).start()

    def _fetch_and_update(self):
        alive = self._is_process_alive()
        stats = None
        if alive:
            try:
                if self._conn is None:
                    self._conn = http.client.HTTPConnection("localhost", BLURB_PORT, timeout=1)
                self._conn.request("GET", "/status")
                resp = self._conn.getresponse()
                stats = json.loads(resp.read())
            except Exception:
                self._conn = None  # reconnect next poll
        # Schedule UI update + next poll on the main thread.
        # Calling root.after from a background thread is unreliable on Linux.
        self.root.after(0, lambda: self._apply_and_reschedule(alive, stats))

    def _apply_and_reschedule(self, alive, stats):
        self._last_alive = alive
        self._update_ui(alive, stats)
        self._update_tray(alive, stats)
        self.root.after(POLL_MS, self._schedule_poll)

    def _update_tray(self, alive: bool, stats: dict | None):
        if self._tray_thread is None:
            return
        if alive:
            job = (stats or {}).get("active_job_id")
            title = "Blurb - transcribing" if job else "Blurb - idle"
        else:
            title = "Blurb - stopped"
        self._tray_thread.set_state(alive, title)

    def _update_ui(self, alive: bool, stats: dict | None):
        if alive:
            self.dot.config(fg=COLOR_RUN)
            self.status_lbl.config(text="Running")
            self.btn.config(text="Stop", bg=COLOR_BTN_STOP)
            if stats:
                job = stats.get("active_job_id") or "idle"
                total = stats.get("jobs_total", 0)
                self.job_lbl.config(text=f"Active job:       {job}")
                self.queue_lbl.config(text=f"Jobs in memory:  {total}")
            else:
                self.job_lbl.config(text="Active job:       starting…")
                self.queue_lbl.config(text="Jobs in memory:  —")
        else:
            self.dot.config(fg=COLOR_STOP)
            self.status_lbl.config(text="Stopped")
            self.btn.config(text="Start", bg=COLOR_BTN_START)
            self.job_lbl.config(text="Active job:       —")
            self.queue_lbl.config(text="Jobs in memory:  —")


if __name__ == "__main__":
    root = tk.Tk()
    BlurbManager(root)
    root.mainloop()
