# SyncHub Control

> A one-window desktop GUI to start/stop Syncthing and see which devices are actually connected.

SyncHub Control is a small single-purpose Windows utility for people running Syncthing (a peer-to-peer file sync tool) who want a simple button-based front end instead of digging through Syncthing's web UI — start it, stop it, and watch connected-device status update live.

## Features
- Start/stop the local Syncthing process from one button.
- Polls Syncthing's local API every 5 seconds to show connected-device status.
- Reads Syncthing's own config XML to auto-discover its settings.
- Detects whether Syncthing is set to launch at Windows startup.
- Logs each session's diagnostics to a dated log file for troubleshooting.

## Stack
Python · CustomTkinter GUI · requests (talks to Syncthing's local REST API) · PyInstaller.

## Getting started
**Requirements**
- Python 3.11+, Windows, Syncthing installed locally (default API at `http://127.0.0.1:8384`).

**Run**
```bash
pip install -r requirements.txt
python main.py
# or run the prebuilt exe from dist/ (SyncHubControl.spec)
```

## Status
**Unmaintained / archived** (last touched 2026-06-17). Published as-is — fork it, adapt it. No support or guarantees.

## License
[MIT](LICENSE) — free to use, fork, and build on.
