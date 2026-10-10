"""Command line: python -m photosync [-c config.toml] run|status"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from . import config as config_mod
from .library import Library
from .rclone import Rclone, RcloneError
from .sync import LibraryUnavailable, human_size, sync

log = logging.getLogger("photosync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="photosync",
        description="Copy photos and videos from cloud drives into one date-sorted library on your NAS.")
    parser.add_argument("-c", "--config", type=Path, default=Path("config.toml"), help="default: config.toml")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every file")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="download new media and add it to the library")
    run_p.add_argument("--dry-run", action="store_true", help="only count what is new; download nothing")
    run_p.add_argument("--source", action="append", metavar="NAME", help="only this source (repeatable)")
    run_p.add_argument("--limit", type=int, metavar="N", help="import at most N new files per source")
    sub.add_parser("status", help="show what the library contains")
    args = parser.parse_args(argv)
    # Filenames can contain anything; don't crash when output goes to a log file with a narrow encoding.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="backslashreplace")

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.command == "run" and args.source:
        unknown = set(args.source) - {s.name for s in cfg.sources}
        if unknown:
            print(f"unknown source(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    _setup_logging(cfg.state_dir / "photosync.log", args.verbose)
    library = Library(cfg.library, cfg.folder_pattern, cfg.state_dir / "photosync.db")
    try:
        if args.command == "status":
            _print_status(cfg, library)
            return 0
        return _run(cfg, library, args)
    finally:
        library.close()


def _run(cfg: config_mod.Config, library: Library, args: argparse.Namespace) -> int:
    selected = [s for s in cfg.sources if (s.name in args.source if args.source else s.enabled)]
    rclone = None
    try:
        if any(s.type == "rclone" for s in selected):
            rclone = Rclone(cfg.rclone, cfg.state_dir / "rclone.log")
            for s in selected:
                if s.type == "rclone":
                    rclone.check_remote(s.location)
        results = sync(cfg, library, rclone, only=args.source, dry_run=args.dry_run, limit=args.limit)
    except (RcloneError, LibraryUnavailable, FileNotFoundError) as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        log.warning("interrupted; everything imported so far is saved, the next run continues from here")
        return 130

    print()
    for name, st in results.items():
        if args.dry_run:
            print(f"{name:20} {st.listed:>8} media files, {st.new:>7} new ({human_size(st.new_bytes)})")
        else:
            print(f"{name:20} {st.imported:>7} imported, {st.duplicates:>7} already in library, {st.failed:>5} failed")
    return 1 if any(st.failed for st in results.values()) else 0


def _print_status(cfg: config_mod.Config, library: Library) -> None:
    s = library.summary()
    count, size, last = s["total"][0]
    print(f"Library:  {cfg.library}")
    print(f"Files:    {count} ({human_size(size)}), last import {last or 'never'}")
    print("\nAdded per source (first source that had the file):")
    for source, n, b in s["by_source"]:
        print(f"  {source:20} {n:>8}  {human_size(b)}")
    print("\nHow the date folder was chosen:")
    for how, n in s["by_date_source"]:
        print(f"  {how:20} {n:>8}")
    print("\nFiles tracked per source:")
    for source, n in s["seen"]:
        print(f"  {source:20} {n:>8}")


def _setup_logging(log_file: Path, verbose: bool) -> None:
    root = logging.getLogger("photosync")
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    file = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file.setLevel(logging.DEBUG)
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)
    root.addHandler(file)


if __name__ == "__main__":
    sys.exit(main())
