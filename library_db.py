"""SQLite library database: one row per downloaded track.

Thread-safe by opening a short-lived connection per operation (the downloader
and the HTTP reader run in different threads).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "library.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    video_id    TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    file_path   TEXT,
    status      TEXT NOT NULL,
    bitrate     INTEGER,
    duration    INTEGER,
    created_at  TEXT
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def init_db() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA)
        conn.commit()


def add_track(video_id: str, title: str, url: str, bitrate: int = 320, duration: int = 0) -> None:
    """Insert a track, or refresh its title/url if it already exists."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tracks (video_id, title, url, file_path, status, bitrate, duration, created_at)
            VALUES (?, ?, ?, NULL, 'downloading', ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET title = excluded.title, url = excluded.url
            """,
            (video_id, title, url, bitrate, duration, _now()),
        )
        conn.commit()


def get_track(video_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
        return dict(row) if row else None


def track_exists(video_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
        return row is not None


def update_track(video_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [video_id]
    with _connect() as conn:
        conn.execute(f"UPDATE tracks SET {cols} WHERE video_id = ?", vals)
        conn.commit()


def set_status(video_id: str, status: str, file_path: Optional[str] = None) -> None:
    update_track(video_id, status=status, file_path=file_path)


def delete_track(video_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM tracks WHERE video_id = ?", (video_id,))
        conn.commit()


def list_tracks() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM tracks ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def count_by_status(status: str) -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM tracks WHERE status = ?", (status,)).fetchone()
        return int(row[0]) if row else 0
