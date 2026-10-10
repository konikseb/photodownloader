"""Thin wrapper around the rclone CLI, which handles cloud logins, listing and downloads."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class RcloneError(Exception):
    pass


@dataclass(frozen=True)
class RemoteFile:
    path: str   # relative to the source root, '/'-separated
    size: int
    mtime: str  # RFC 3339 timestamp as reported by the source


class Rclone:
    def __init__(self, exe: str, log_file: Path):
        resolved = shutil.which(exe)
        if not resolved:
            raise RcloneError(f"rclone not found ({exe!r}). Install it with: winget install Rclone.Rclone")
        self.exe = resolved
        self.log_file = log_file

    def _run(self, *args: str) -> str:
        proc = subprocess.run([self.exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RcloneError(f"rclone {args[0]} failed (exit {proc.returncode}): {proc.stderr.strip()[-2000:]}")
        return proc.stdout

    def check_remote(self, remote: str) -> None:
        name = remote.split(":", 1)[0] + ":"
        if name not in self._run("listremotes").split():
            raise RcloneError(f"rclone has no remote called {name!r}; create it with: rclone config")

    def list_files(self, remote: str) -> list[RemoteFile]:
        out = self._run("lsjson", remote, "--recursive", "--files-only", "--no-mimetype", "--fast-list")
        return [RemoteFile(e["Path"], int(e["Size"]), e["ModTime"]) for e in json.loads(out) if not e.get("IsDir")]

    def download(self, remote: str, paths: list[str], dest: Path, list_file: Path) -> None:
        """Copy the given files from remote into dest, keeping their relative paths.

        Files that fail to download are logged by rclone and simply won't exist in dest;
        the caller treats them as not-yet-imported so the next run retries them.
        """
        list_file.write_text("\n".join(paths) + "\n", encoding="utf-8")
        proc = subprocess.run([
            self.exe, "copy", remote, str(dest),
            "--files-from-raw", str(list_file),
            "--no-traverse",  # fetch just these files instead of re-listing the whole remote every batch
            "--log-file", str(self.log_file), "--log-level", "INFO",
        ])
        if proc.returncode != 0:
            log.warning("rclone copy exited with %s; see %s", proc.returncode, self.log_file)


# rclone's local backend swaps characters Windows doesn't allow in names for look-alikes.
_WIN_RESERVED = '<>:"|?*\\'
_WIN_REPLACEMENTS = {c: chr(ord(c) + 0xFEE0) for c in _WIN_RESERVED}  # fullwidth forms, e.g. ':' -> '：'
_CTL_REPLACEMENTS = {chr(i): chr(0x2400 + i) for i in range(0x20)}    # e.g. '\t' -> '␉'
_QUOTE = "‛"  # rclone prefixes look-alikes that were already in the name with this


def local_path(remote_path: str) -> Path:
    """Where `rclone copy` stores remote_path on this machine, relative to the destination folder."""
    segments = remote_path.split("/")
    if os.name != "nt":
        return Path(*segments)
    already_encoded = set(_WIN_REPLACEMENTS.values()) | set(_CTL_REPLACEMENTS.values())
    encoded = []
    for seg in segments:
        out = []
        for c in seg:
            if c in already_encoded:
                out.append(_QUOTE + c)
            else:
                out.append(_WIN_REPLACEMENTS.get(c) or _CTL_REPLACEMENTS.get(c) or c)
        seg = "".join(out)
        if seg.endswith(" "):
            seg = seg[:-1] + "␠"  # ␠
        elif seg.endswith("."):
            seg = seg[:-1] + "．"  # ．
        encoded.append(seg)
    return Path(*encoded)
