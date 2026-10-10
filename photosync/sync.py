"""Pull new media from every source into the library."""
from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, Source
from .library import Library
from .rclone import RemoteFile, local_path

log = logging.getLogger(__name__)


class LibraryUnavailable(Exception):
    pass


@dataclass
class Stats:
    listed: int = 0
    new: int = 0
    new_bytes: int = 0
    imported: int = 0
    duplicates: int = 0
    failed: int = 0


def sync(cfg: Config, library: Library, rclone, *, only: list[str] | None = None, dry_run: bool = False,
         limit: int | None = None) -> dict[str, Stats]:
    """Import everything new. Sources are only ever read; nothing is deleted anywhere."""
    if not dry_run:
        try:
            cfg.library.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise LibraryUnavailable(f"library folder {cfg.library} is not reachable: {e}") from e

    results: dict[str, Stats] = {}
    for source in cfg.sources:
        # Naming a source with --source runs it even if it is disabled in the config.
        if (only and source.name not in only) or (not only and not source.enabled):
            continue
        log.info("%s: listing %s", source.name, source.location)
        files = rclone.list_files(source.location) if source.type == "rclone" else _list_folder(Path(source.location))
        media = [f for f in files if _is_media(f.path, cfg.extensions)]
        seen = library.seen(source.name)
        new = sorted((f for f in media if seen.get(f.path) != (f.size, f.mtime)), key=lambda f: f.path)
        if limit is not None:
            new = new[:limit]
        stats = results[source.name] = Stats(listed=len(media), new=len(new), new_bytes=sum(f.size for f in new))
        log.info("%s: %d media files, %d new (%s)", source.name, stats.listed, stats.new, human_size(stats.new_bytes))
        if dry_run or not new:
            continue
        if source.type == "rclone":
            _import_from_rclone(source, new, cfg, library, rclone, stats)
        else:
            _import_from_folder(source, new, cfg, library, stats)
        log.info("%s: imported %d, already in library %d, failed %d",
                 source.name, stats.imported, stats.duplicates, stats.failed)
    return results


def _import_from_rclone(source: Source, files: list[RemoteFile], cfg: Config, library: Library, rclone,
                        stats: Stats) -> None:
    # Download in batches so staging never holds more than batch_size files at once.
    staging = cfg.staging_dir / source.name
    list_file = cfg.state_dir / f"{source.name}.files.txt"
    for start in range(0, len(files), cfg.batch_size):
        batch = files[start:start + cfg.batch_size]
        shutil.rmtree(_long_path(staging), ignore_errors=True)
        staging.mkdir(parents=True)
        rclone.download(source.location, [f.path for f in batch], staging, list_file)
        for f in batch:
            local = _long_path(staging / local_path(f.path))
            if not local.is_file():
                log.warning("%s: %s did not download; it will be retried next run", source.name, f.path)
                stats.failed += 1
                continue
            try:
                _import_one(local, source, f, library, stats)
            finally:
                local.unlink(missing_ok=True)
        leftovers = [p for p in _long_path(staging).rglob("*") if p.is_file()]
        if leftovers:
            log.warning("%s: %d downloaded files could not be matched to their source path, e.g. %s",
                        source.name, len(leftovers), leftovers[0])
        log.info("%s: %d/%d processed", source.name, start + len(batch), len(files))
    shutil.rmtree(_long_path(staging), ignore_errors=True)


def _import_from_folder(source: Source, files: list[RemoteFile], cfg: Config, library: Library,
                        stats: Stats) -> None:
    root = Path(source.location)
    for i, f in enumerate(files, 1):
        _import_one(_long_path(root.joinpath(*f.path.split("/"))), source, f, library, stats)
        if i % cfg.batch_size == 0 or i == len(files):
            log.info("%s: %d/%d processed", source.name, i, len(files))


def _import_one(file: Path, source: Source, f: RemoteFile, library: Library, stats: Stats) -> None:
    try:
        result = library.import_file(file, source=source.name, source_path=f.path, size=f.size, mtime=f.mtime,
                                     mtime_dt=_parse_time(f.mtime))
    except OSError as e:
        if not library.root.exists():
            raise LibraryUnavailable(f"library folder {library.root} is not reachable: {e}") from e
        log.error("%s: could not import %s: %s", source.name, f.path, e)
        stats.failed += 1
        return
    if result.status == "imported":
        stats.imported += 1
        log.debug("%s: %s -> %s", source.name, f.path, result.library_path)
    else:
        stats.duplicates += 1
        log.debug("%s: %s is already in the library as %s", source.name, f.path, result.library_path)


def _list_folder(root: Path) -> list[RemoteFile]:
    if not root.is_dir():
        raise FileNotFoundError(f"source folder not found: {root}")
    files = []
    for dirpath, dirnames, filenames in os.walk(_long_path(root)):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath, name)
            st = p.stat()
            mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
            files.append(RemoteFile(p.relative_to(_long_path(root)).as_posix(), st.st_size, mtime))
    return files


def _is_media(path: str, extensions: frozenset[str]) -> bool:
    parts = path.split("/")
    # Hidden files and folders: thumbnails, Android's .trashed-* files, macOS ._ resource forks.
    if any(part.startswith(".") for part in parts):
        return False
    return os.path.splitext(parts[-1])[1].lower().lstrip(".") in extensions


def _parse_time(value: str) -> datetime | None:
    """RFC 3339 from rclone (possibly with nanoseconds) -> naive local time."""
    try:
        when = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value))
    except ValueError:
        return None
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


def _long_path(p: Path) -> Path:
    """Let Windows open paths longer than 260 characters (deeply nested cloud folders)."""
    s = str(p)
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return p
    return Path("\\\\?\\UNC\\" + s[2:]) if s.startswith("\\\\") else Path("\\\\?\\" + s)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
