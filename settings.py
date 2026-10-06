"""Simple JSON-backed settings, reused by the Phase 3 settings UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = ROOT / "settings.json"

DEFAULTS: dict[str, Any] = {
    "bitrate": 320,          # MP3 quality: 128 / 192 / 320
    "library_dir": None,     # None = default (Music folder)
    "memory_mode": False,    # don't save new audios to disk
    "queue_depth": 3,        # how many queue items to pre-download (0 = off)
    "download_workers": 3,   # total encode lanes: 1 foreground + rest background
    "session_queue": [],     # last session's queue items (id/title/channel/duration/thumbnail)
    "session_qindex": -1,    # index of current track in session_queue
    "offline_mode": False,   # zero network, local library only
}


def load() -> dict[str, Any]:
    data = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                data.update(saved)
        except (OSError, json.JSONDecodeError):
            pass
    return data


def save(data: dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get(key: str) -> Any:
    return load().get(key, DEFAULTS.get(key))


def set(key: str, value: Any) -> None:
    data = load()
    data[key] = value
    save(data)
