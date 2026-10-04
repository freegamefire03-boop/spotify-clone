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
    created_at      TEXT,
    play_count      INTEGER NOT NULL DEFAULT 0,
    last_played_at  TEXT
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
    if "play_count" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0")
    if "last_played_at" not in existing:
        conn.execute("ALTER TABLE tracks ADD COLUMN last_played_at TEXT")
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


def record_play(video_id: str) -> None:
    """Bump play_count + last_played_at. Assumes the row exists."""
    with _connect() as conn:
        conn.execute(
            "UPDATE tracks SET play_count = COALESCE(play_count, 0) + 1, "
            "last_played_at = ? WHERE video_id = ?",
            (_now(), video_id),
        )
        conn.commit()


def list_recently_played(limit: int = 10) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE last_played_at IS NOT NULL "
            "ORDER BY last_played_at DESC, COALESCE(play_count, 0) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


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


def _try_unlink(p: Path) -> bool:
    """Delete one file; return True on success, False on any failure."""
    try:
        p.unlink()
        return True
    except OSError:
        return False


def reconcile_stale_downloads() -> dict[str, int]:
    """Reconcile unfinished work from previous sessions on startup.

    - Stale 'downloading' rows are repaired (complete file on disk) or failed.
    - Leftover `.part` files are deleted: nothing resumes today, so at
      startup no `.part` belongs to a live download. Completed `.mp3` files
      and non-`.part` files are never touched.
    - Memory-only rows are dropped (their buffers died with the process).

    Returns counts of {repaired: N, failed: M, parts_deleted: K}.
    """
    repaired = 0
    failed = 0
    parts_deleted = 0
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
        # DB-driven pass: .part files of rows that will never resume
        # (downloading rows failed above, plus Cancelled/failed leftovers —
        # cancel() abandons the .part without deleting it).
        stale = conn.execute(
            "SELECT file_path FROM tracks WHERE status IN ('downloading', 'cancelled', 'failed')"
        ).fetchall()
        for row in stale:
            file_path = row["file_path"]
            if not file_path:
                continue
            candidate = Path(str(file_path) + ".part")
            try:
                if candidate.is_file() and _try_unlink(candidate):
                    parts_deleted += 1
            except OSError:
                pass
        conn.execute("DELETE FROM tracks WHERE status = 'memory'")
        conn.commit()
    # Folder sweep: orphans with no DB row (row deleted, collision rename,
    # or a failed delete inside the downloader). Only *.part matches, so
    # completed .mp3 files and anything else survive by construction.
    # NOTE: raw `python main.py` twice bypasses start.bat's single-instance
    # guard; the sweep could then delete a sibling's growing .part (it would
    # just re-download). Accepted: .mp3 files are never touched.
    try:
        from library import get_library_dir

        lib = get_library_dir()
    except OSError:
        lib = None
    if lib is not None:
        try:
            for p in lib.glob("*.part"):
                try:
                    if p.is_file() and _try_unlink(p):
                        parts_deleted += 1
                except OSError:
                    pass
        except OSError:
            pass
    return {"repaired": repaired, "failed": failed, "parts_deleted": parts_deleted}


def delete_track(video_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM tracks WHERE video_id = ?", (video_id,))
        conn.commit()


def list_tracks() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM tracks ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def list_home_library(limit: int = 20) -> list[dict[str, Any]]:
    """Recently-played completions first, then most-recent rows, deduped."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE status = 'mp3' "
            "ORDER BY COALESCE(completed_at, created_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        ids = {r["video_id"] for r in rows}
        if len(rows) < limit:
            extra = conn.execute(
                "SELECT * FROM tracks ORDER BY COALESCE(completed_at, created_at) DESC LIMIT ?",
                (limit,),
            ).fetchall()
            rows = list(rows)
            for r in extra:
                if r["video_id"] not in ids:
                    rows.append(r)
                    ids.add(r["video_id"])
                if len(rows) >= limit:
                    break
        return [dict(r) for r in rows[:limit]]


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
