"""The date-sorted library on the NAS and the SQLite index that tracks what is in it."""
from __future__ import annotations

import hashlib
import itertools
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import metadata

UNKNOWN_DATE_FOLDER = "_unknown-date"

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    sha256       TEXT PRIMARY KEY,
    library_path TEXT NOT NULL,   -- relative to the library root, '/'-separated
    size         INTEGER NOT NULL,
    taken_at     TEXT,
    date_source  TEXT NOT NULL,   -- exif | video | filename | mtime | unknown
    first_source TEXT NOT NULL,
    imported_at  TEXT NOT NULL
);
-- Every file seen in a source, so unchanged files are skipped without downloading them again.
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL,
    path   TEXT NOT NULL,
    size   INTEGER NOT NULL,
    mtime  TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (source, path)
);
"""


@dataclass(frozen=True)
class ImportResult:
    status: str        # "imported" or "duplicate"
    library_path: str


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Library:
    def __init__(self, root: Path, folder_pattern: str, db_path: Path):
        self.root = root
        self.folder_pattern = folder_pattern
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def seen(self, source: str) -> dict[str, tuple[int, str]]:
        """path -> (size, mtime) for everything already imported from source."""
        rows = self.db.execute("SELECT path, size, mtime FROM seen WHERE source = ?", (source,))
        return {path: (size, mtime) for path, size, mtime in rows}

    def import_file(self, file: Path, *, source: str, source_path: str, size: int, mtime: str,
                    mtime_dt: datetime | None) -> ImportResult:
        """Copy file into the library unless identical content is already there. Never touches file itself."""
        digest = sha256_of(file)
        row = self.db.execute("SELECT library_path FROM media WHERE sha256 = ?", (digest,)).fetchone()
        if row:
            with self.db:
                self._mark_seen(source, source_path, size, mtime, digest)
            return ImportResult("duplicate", row[0])

        taken, how = metadata.date_taken(file, mtime_dt)
        folder = taken.strftime(self.folder_pattern) if taken else UNKNOWN_DATE_FOLDER
        library_path = self._place(file, folder, digest)
        with self.db:
            self.db.execute(
                "INSERT INTO media VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest, library_path, file.stat().st_size, taken.isoformat(sep=" ") if taken else None, how,
                 source, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self._mark_seen(source, source_path, size, mtime, digest)
        return ImportResult("imported", library_path)

    def _mark_seen(self, source: str, path: str, size: int, mtime: str, digest: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?, ?)", (source, path, size, mtime, digest))

    def _place(self, file: Path, folder: str, digest: str) -> str:
        target_dir = self.root / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        stem, suffix = os.path.splitext(file.name)
        for n in itertools.count():
            name = file.name if n == 0 else f"{stem}_{n}{suffix}"
            target = target_dir / name
            if not target.exists():
                break
            # Same content already sitting there (e.g. the index was deleted and rebuilt): reuse it.
            if target.stat().st_size == file.stat().st_size and sha256_of(target) == digest:
                return target.relative_to(self.root).as_posix()
        # Copy under a temporary name so an interrupted run never leaves a half-written photo.
        partial = target_dir / f".{name}.partial"
        shutil.copy2(file, partial)
        os.replace(partial, target)
        return target.relative_to(self.root).as_posix()

    def summary(self) -> dict[str, list[tuple]]:
        q = self.db.execute
        return {
            "total": q("SELECT COUNT(*), COALESCE(SUM(size), 0), MAX(imported_at) FROM media").fetchall(),
            "by_source": q("SELECT first_source, COUNT(*), SUM(size) FROM media GROUP BY 1 ORDER BY 1").fetchall(),
            "by_date_source": q("SELECT date_source, COUNT(*) FROM media GROUP BY 1 ORDER BY 2 DESC").fetchall(),
            "seen": q("SELECT source, COUNT(*) FROM seen GROUP BY 1 ORDER BY 1").fetchall(),
        }
