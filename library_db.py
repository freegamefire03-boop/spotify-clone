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
    video_id        TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    file_path       TEXT,
    status          TEXT NOT NULL,
    bitrate         INTEGER,
    duration        INTEGER,
    file_size       INTEGER,
    completed_at    TEXT,
    created_at      TEXT
)
"""

MIGRATION_ADD_COLUMNS = """
ALTER TABLE tracks ADD COLUMN file_size INTEGER;
ALTER TABLE tracks ADD COLUMN completed_at TEXT;
"""


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add new columns if they don't exist (idempotent)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    if "file_size" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN file_size INTEGER")
    if "completed_at" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN completed_at TEXT")
    conn.commit()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def init_db() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA)
        _migrate_schema(conn)
        conn.commit()


def add_track(video_id: str, title: str, url: str, bitrate: int = 320, duration: int = 0) -> None:
    """Insert a track, or refresh its title/url if it already exists.

    Preserves file_path and status on conflict — never wipes existing state.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tracks (video_id, title, url, file_path, status, bitrate, duration, created_at)
            VALUES (?, ?, ?, NULL, 'downloading', ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title = excluded.title,
                url = excluded.url
            """,
            (video_id, title, url, bitrate, duration, _now()),
        )
        conn.commit()


def get_or_create_track(
    video_id: str, title: str, url: str, bitrate: int = 320, duration: int = 0
) -> dict[str, Any]:
    """Upsert track metadata and return the current row.

    Returns the existing row with file_path/status intact on conflict.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tracks (video_id, title, url, file_path, status, bitrate, duration, created_at)
            VALUES (?, ?, ?, NULL, 'downloading', ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title = excluded.title,
                url = excluded.url
            """,
            (video_id, title, url, bitrate, duration, _now()),
        )
        row = conn.execute("SELECT * FROM tracks WHERE video_id = ?", (video_id,)).fetchone()
        conn.commit()
        return dict(row) if row else {}


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


def set_status(
    video_id: str,
    status: str,
    file_path: Optional[str] = None,
    file_size: Optional[int] = None,
) -> None:
    fields: dict[str, Any] = {"status": status}
    if file_path is not None:
        fields["file_path"] = file_path
    if file_size is not None:
        fields["file_size"] = file_size
    if status == "mp3":
        fields["completed_at"] = _now()
    update_track(video_id, **fields)


def reconcile_stale_downloads() -> dict[str, int]:
    """Mark stale 'downloading' rows as 'failed' on startup.

    Returns counts of {repaired: N, failed: M}.
    """
    repaired = 0
    failed = 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT video_id, file_path FROM tracks WHERE status = 'downloading'"
        ).fetchall()
        for row in rows:
            video_id = row["video_id"]
            file_path = row["file_path"]
            if file_path and Path(file_path).is_file():
                size = Path(file_path).stat().st_size
                if size > 0:
                    conn.execute(
                        "UPDATE tracks SET status = 'mp3', file_size = ?, completed_at = ? WHERE video_id = ?",
                        (size, _now(), video_id),
                    )
                    repaired += 1
                    continue
            conn.execute(
                "UPDATE tracks SET status = 'failed' WHERE video_id = ?",
                (video_id,),
            )
            failed += 1
        conn.execute("DELETE FROM tracks WHERE status = 'memory'")
        conn.commit()
    return {"repaired": repaired, "failed": failed}


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


def cleanup_memory_tracks() -> int:
    """Remove memory-only tracks from DB (call on app exit)."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM tracks WHERE status = 'memory'")
        conn.commit()
        return cur.rowcount
