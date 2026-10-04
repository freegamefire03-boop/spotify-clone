"""Library storage: folder resolution, filename sanitization, path helpers."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "library.config"

_ILLEGAL = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")


def get_library_dir() -> Path:
    """The folder where audio is stored. Defaults to the user's Music folder."""
    if CONFIG_PATH.exists():
        saved = CONFIG_PATH.read_text(encoding="utf-8").strip()
        if saved:
            p = Path(saved)
            if p.is_dir():
                return p
    music = Path.home() / "Music"
    music.mkdir(parents=True, exist_ok=True)
    return music


def validate_library_dir(path: str | Path) -> Path:
    """Check a library folder is usable: creatable + writable."""
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("folder path is empty")
    p = Path(raw).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"folder is not creatable/writable: {raw} ({exc})") from exc
    return p.resolve()


def set_library_dir(path: str | Path) -> Path:
    p = validate_library_dir(path)
    CONFIG_PATH.write_text(str(p), encoding="utf-8")
    return p


def sanitize_filename(title: str) -> str:
    """Make a YouTube title safe for a Windows filename (no extension)."""
    name = _ILLEGAL.sub("", title)
    name = _WHITESPACE.sub(" ", name).strip().rstrip(".")
    name = name[:120].strip()
    return name


def disk_free(path: str | Path) -> int:
    return shutil.disk_usage(path).free
