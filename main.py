#!/usr/bin/env python3
"""SyncHub Control — manually start/stop Syncthing, see connected devices."""

import os
import sys
import time
import queue
import threading
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime

import customtkinter as ctk
import requests

# ── Diagnostics ────────────────────────────────────────────────────────────
APP_NAME = "SyncHubControl"
APP_VERSION = "1.0.0"
_LOG_DIR = Path.home() / ".claude" / "session-data" / datetime.now().strftime("%Y-%m-%d")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / f"exe_{APP_NAME}.log"


def log(level: str, msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} {APP_NAME} [{level}] {msg}\n")
    except Exception:
        pass


def bootstrap() -> None:
    log("STARTUP", (
        f"{APP_NAME} v{APP_VERSION} "
        f"python={sys.version.split()[0]} "
        f"frozen={getattr(sys, 'frozen', False)} "
        f"argv={sys.argv}"
    ))


# ── Constants ──────────────────────────────────────────────────────────────
API_BASE = "http://127.0.0.1:8384"
POLL_INTERVAL = 5  # seconds

STARTUP_SHORTCUT = (
    Path(os.environ.get("APPDATA", ""))
    / "Microsoft" / "Windows" / "Start Menu"
    / "Programs" / "Startup" / "Syncthing.lnk"
)


# ── Config Discovery ───────────────────────────────────────────────────────
def find_config() -> Path | None:
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(local) / "Syncthing" / "config.xml",
        Path.home() / ".config" / "syncthing" / "config.xml",
        Path.home() / ".local" / "state" / "syncthing" / "config.xml",
    ]
    for p in candidates:
        if p.exists():
            log("STATE", f"config at {p}")
            return p
    log("ERROR", "config.xml not found")
    return None


def read_api_key(config: Path) -> str | None:
    try:
        root = ET.parse(config).getroot()
        gui = root.find("gui")
        if gui is not None:
            key = gui.find("apikey")
            if key is not None:
                return key.text
    except Exception as exc:
        log("ERROR", f"api key read failed: {exc}")
    return None


def find_syncthing_exe() -> Path | None:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    # WinGet
    winget = local / "Microsoft" / "WinGet" / "Packages"
    if winget.exists():
        for d in winget.iterdir():
            if d.name.startswith("Syncthing.Syncthing"):
                exe = d / "syncthing.exe"
                if exe.exists():
                    log("DECISION", f"exe via winget: {exe}")
                    return exe
    # Manual install
    manual = local / "Syncthing" / "syncthing.exe"
    if manual.exists():
        return manual
    # PATH
    import shutil
    found = shutil.which("syncthing")
    if found:
        return Path(found)
    log("ERROR", "syncthing.exe not found")
    return None


# ── Syncthing API ──────────────────────────────────────────────────────────
def _api_get(endpoint: str, api_key: str) -> dict | list | None:
    try:
        r = requests.get(
            f"{API_BASE}{endpoint}",
            headers={"X-API-Key": api_key},
            timeout=2,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def api_ping(api_key: str) -> bool:
    return _api_get("/rest/system/ping", api_key) is not None


def api_connections(api_key: str) -> dict:
    data = _api_get("/rest/system/connections", api_key)
    if isinstance(data, dict):
        return data.get("connections", {})
    return {}


def api_devices(api_key: str) -> list:
    data = _api_get("/rest/config/devices", api_key)
    return data if isinstance(data, list) else []


def api_folder_status(api_key: str) -> dict | None:
    data = _api_get("/rest/db/status?folder=synchub", api_key)
    return data if isinstance(data, dict) else None


def api_my_id(api_key: str) -> str | None:
    data = _api_get("/rest/system/status", api_key)
    if isinstance(data, dict):
        return data.get("myID")
    return None


def api_shutdown(api_key: str) -> None:
    try:
        requests.post(
            f"{API_BASE}/rest/system/shutdown",
            headers={"X-API-Key": api_key},
            timeout=2,
        )
        log("STATE", "shutdown via API")
    except Exception:
        _kill_syncthing()


def _kill_syncthing() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "syncthing.exe"],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    log("STATE", "syncthing killed via taskkill")


def launch_syncthing(exe: Path) -> None:
    subprocess.Popen(
        [str(exe), "serve", "--no-browser"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log("STATE", f"syncthing launched from {exe}")


# ── Helpers ────────────────────────────────────────────────────────────────
def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def short_id(device_id: str) -> str:
    return device_id[:7] if device_id else "?"


# ── GUI ────────────────────────────────────────────────────────────────────
class SyncHubApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SyncHub Control")
        self.geometry("420x600")
        self.resizable(False, False)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # internal state
        self._api_key: str | None = None
        self._exe: Path | None = None
        self._running = False
        self._my_id: str | None = None
        self._stop_poll = threading.Event()
        self._q: queue.Queue = queue.Queue()
        self._device_rows: dict[str, dict] = {}

        self._load_config()
        self._remove_autostart()
        self._build_ui()
        self._initial_check()
        self._drain_queue()

    # ── Setup ──────────────────────────────────────────────────────────────
    def _load_config(self) -> None:
        cfg = find_config()
        if cfg:
            self._api_key = read_api_key(cfg)
        self._exe = find_syncthing_exe()
        log("DECISION", f"api_key={'ok' if self._api_key else 'missing'} exe={'ok' if self._exe else 'missing'}")

    def _remove_autostart(self) -> None:
        if STARTUP_SHORTCUT.exists():
            STARTUP_SHORTCUT.unlink()
            log("STATE", "removed autostart shortcut")

    # ── UI Build ───────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # Header
        hdr = ctk.CTkFrame(self, fg_color="#0d1117", corner_radius=0)
        hdr.pack(fill="x")

        ctk.CTkLabel(
            hdr, text="⟳  SyncHub Control",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#58a6ff",
        ).pack(pady=(18, 4))

        self._status_lbl = ctk.CTkLabel(
            hdr, text="Checking…",
            font=ctk.CTkFont(size=13),
            text_color="#8b949e",
        )
        self._status_lbl.pack(pady=(0, 18))

        # Toggle button
        self._toggle_btn = ctk.CTkButton(
            self, text="● START", width=280, height=60,
            font=ctk.CTkFont(size=18, weight="bold"),
            fg_color="#238636", hover_color="#2ea043",
            command=self._toggle,
        )
        self._toggle_btn.pack(pady=20)

        # Devices
        ctk.CTkLabel(
            self, text="CONNECTED DEVICES",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#484f58",
        ).pack(anchor="w", padx=20, pady=(4, 2))

        self._dev_frame = ctk.CTkScrollableFrame(
            self, height=170, fg_color="#0d1117",
        )
        self._dev_frame.pack(fill="x", padx=16, pady=(0, 12))

        # Folder
        ctk.CTkLabel(
            self, text="SYNCHUB FOLDER",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color="#484f58",
        ).pack(anchor="w", padx=20, pady=(4, 2))

        fold = ctk.CTkFrame(self, fg_color="#0d1117", corner_radius=8)
        fold.pack(fill="x", padx=16, pady=(0, 12))

        self._fold_status = ctk.CTkLabel(
            fold, text="—",
            font=ctk.CTkFont(size=14),
            anchor="w",
        )
        self._fold_status.pack(anchor="w", padx=14, pady=(10, 2))

        self._fold_detail = ctk.CTkLabel(
            fold, text="",
            font=ctk.CTkFont(size=11),
            text_color="#8b949e", anchor="w",
        )
        self._fold_detail.pack(anchor="w", padx=14, pady=(0, 10))

        # Footer — my device ID
        self._myid_lbl = ctk.CTkLabel(
            self, text="",
            font=ctk.CTkFont(size=10),
            text_color="#30363d",
        )
        self._myid_lbl.pack(pady=(0, 10))

    # ── Initial State ──────────────────────────────────────────────────────
    def _initial_check(self) -> None:
        def _check() -> None:
            running = bool(self._api_key and api_ping(self._api_key))
            self._q.put(("initial", running))
        threading.Thread(target=_check, daemon=True).start()

    # ── Toggle ─────────────────────────────────────────────────────────────
    def _toggle(self) -> None:
        self._toggle_btn.configure(state="disabled", text="Working…")
        if self._running:
            threading.Thread(target=self._do_stop, daemon=True).start()
        else:
            threading.Thread(target=self._do_start, daemon=True).start()

    def _do_start(self) -> None:
        if not self._exe:
            self._q.put(("error", "syncthing.exe not found — reinstall Syncthing"))
            return
        launch_syncthing(self._exe)
        # Wait up to 10s for API
        for _ in range(20):
            time.sleep(0.5)
            if self._api_key and api_ping(self._api_key):
                self._q.put(("started", None))
                return
        self._q.put(("error", "Started but API didn't respond in 10s"))

    def _do_stop(self) -> None:
        if self._api_key:
            api_shutdown(self._api_key)
        else:
            _kill_syncthing()
        time.sleep(1.5)
        self._q.put(("stopped", None))

    # ── Poll Loop ──────────────────────────────────────────────────────────
    def _start_polling(self) -> None:
        self._stop_poll.clear()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _stop_polling(self) -> None:
        self._stop_poll.set()

    def _poll_loop(self) -> None:
        while not self._stop_poll.is_set():
            self._fetch()
            self._stop_poll.wait(POLL_INTERVAL)

    def _fetch(self) -> None:
        if not self._api_key:
            return
        if not api_ping(self._api_key):
            self._q.put(("stopped", None))
            return
        my_id = self._my_id or api_my_id(self._api_key)
        if my_id:
            self._my_id = my_id
        self._q.put(("update", {
            "devices": api_devices(self._api_key),
            "connections": api_connections(self._api_key),
            "folder": api_folder_status(self._api_key),
            "my_id": my_id,
        }))

    # ── Queue / UI Update ──────────────────────────────────────────────────
    def _drain_queue(self) -> None:
        try:
            while True:
                msg, data = self._q.get_nowait()
                self._handle(msg, data)
        except queue.Empty:
            pass
        self.after(200, self._drain_queue)

    def _handle(self, msg: str, data) -> None:
        if msg == "initial":
            self._set_state(data)
            if data:
                self._start_polling()

        elif msg == "started":
            self._set_state(True)
            self._start_polling()

        elif msg == "stopped":
            self._stop_polling()
            self._set_state(False)
            self._clear_devices()
            self._fold_status.configure(text="—", text_color="white")
            self._fold_detail.configure(text="")

        elif msg == "error":
            self._set_state(False)
            self._status_lbl.configure(text=f"⚠  {data}", text_color="#f85149")
            self._toggle_btn.configure(state="normal")

        elif msg == "update":
            self._refresh(data)

    def _set_state(self, running: bool) -> None:
        self._running = running
        if running:
            self._status_lbl.configure(text="● Running", text_color="#3fb950")
            self._toggle_btn.configure(
                text="■  STOP",
                fg_color="#b91c1c", hover_color="#991b1b",
                state="normal",
            )
        else:
            self._status_lbl.configure(text="○ Stopped", text_color="#8b949e")
            self._toggle_btn.configure(
                text="●  START",
                fg_color="#238636", hover_color="#2ea043",
                state="normal",
            )

    # ── Device Rows ────────────────────────────────────────────────────────
    def _clear_devices(self) -> None:
        for w in self._dev_frame.winfo_children():
            w.destroy()
        self._device_rows.clear()

    def _ensure_row(self, dev_id: str, name: str) -> dict:
        if dev_id in self._device_rows:
            return self._device_rows[dev_id]
        # Remove "no devices" placeholder
        if "__ph__" in self._device_rows:
            self._device_rows["__ph__"]["frame"].destroy()
            del self._device_rows["__ph__"]

        frame = ctk.CTkFrame(self._dev_frame, fg_color="#161b22", corner_radius=6)
        frame.pack(fill="x", pady=3, padx=2)

        name_lbl = ctk.CTkLabel(
            frame, text=name,
            font=ctk.CTkFont(size=13, weight="bold"), anchor="w",
        )
        name_lbl.pack(anchor="w", padx=10, pady=(8, 0))

        status_lbl = ctk.CTkLabel(
            frame, text="Checking…",
            font=ctk.CTkFont(size=11), text_color="#8b949e", anchor="w",
        )
        status_lbl.pack(anchor="w", padx=10, pady=(0, 2))

        addr_lbl = ctk.CTkLabel(
            frame, text="",
            font=ctk.CTkFont(size=10), text_color="#30363d", anchor="w",
        )
        addr_lbl.pack(anchor="w", padx=10, pady=(0, 8))

        row = {"frame": frame, "status": status_lbl, "addr": addr_lbl}
        self._device_rows[dev_id] = row
        return row

    def _ensure_placeholder(self) -> None:
        if self._device_rows:
            return
        lbl = ctk.CTkLabel(
            self._dev_frame, text="No paired devices",
            text_color="#30363d", font=ctk.CTkFont(size=12),
        )
        lbl.pack(pady=24)
        self._device_rows["__ph__"] = {"frame": lbl, "status": lbl, "addr": lbl}

    # ── Refresh Display ────────────────────────────────────────────────────
    def _refresh(self, data: dict) -> None:
        devices: list = data.get("devices", [])
        conns: dict = data.get("connections", {})
        folder: dict | None = data.get("folder")
        my_id: str | None = data.get("my_id")

        if my_id:
            self._myid_lbl.configure(text=f"My ID: {short_id(my_id)}…")

        seen: set[str] = set()
        for dev in devices:
            dev_id = dev.get("deviceID", "")
            if dev_id == my_id:
                continue
            seen.add(dev_id)
            name = dev.get("name") or short_id(dev_id)
            conn = conns.get(dev_id, {})
            connected = conn.get("connected", False)
            row = self._ensure_row(dev_id, name)
            if connected:
                row["status"].configure(text="● Connected", text_color="#3fb950")
                row["addr"].configure(text=conn.get("address", "")[:50])
            else:
                row["status"].configure(text="○ Disconnected", text_color="#484f58")
                row["addr"].configure(text="")

        # Remove stale rows
        for dev_id in [k for k in self._device_rows if k not in seen and k != "__ph__"]:
            self._device_rows[dev_id]["frame"].destroy()
            del self._device_rows[dev_id]

        if not seen:
            self._ensure_placeholder()

        # Folder
        if folder:
            state = folder.get("state", "unknown")
            files = folder.get("localFiles", 0)
            size = folder.get("localBytes", 0)
            need_files = folder.get("needFiles", 0)
            global_bytes = folder.get("globalBytes", 1)
            need_bytes = folder.get("needBytes", 0)

            if state == "idle":
                self._fold_status.configure(text="✓  Up to Date", text_color="#3fb950")
            elif state == "syncing":
                pct = max(0, (1 - need_bytes / max(global_bytes, 1)) * 100)
                self._fold_status.configure(
                    text=f"⟳  Syncing ({pct:.0f}%)", text_color="#d29922",
                )
            elif state == "scanning":
                self._fold_status.configure(text="⟳  Scanning…", text_color="#d29922")
            else:
                self._fold_status.configure(text=state.capitalize(), text_color="#8b949e")

            extra = f"  ·  {need_files} needed" if need_files else ""
            self._fold_detail.configure(
                text=f"{files:,} files  ·  {fmt_bytes(size)}{extra}",
            )

    # ── Close ──────────────────────────────────────────────────────────────
    def on_close(self) -> None:
        self._stop_polling()
        log("STATE", "ready→shutdown")
        self.destroy()


# ── Entry ──────────────────────────────────────────────────────────────────
def main() -> None:
    bootstrap()
    log("STATE", "init→ready")
    app = SyncHubApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
