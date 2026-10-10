"""Load and validate config.toml."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXTENSIONS = frozenset({
    # photos
    "jpg", "jpeg", "png", "heic", "heif", "gif", "bmp", "tif", "tiff", "webp",
    # raw photos
    "dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "srw",
    # videos
    "mp4", "mov", "m4v", "3gp", "avi", "mkv", "mts", "m2ts", "wmv", "webm",
})


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Source:
    name: str
    type: str       # "rclone" or "folder"
    location: str   # rclone remote such as "gdrive:Photos", or an absolute folder path
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    library: Path
    folder_pattern: str
    state_dir: Path
    staging_dir: Path
    rclone: str
    batch_size: int
    extensions: frozenset[str]
    sources: tuple[Source, ...]


def load(path: Path) -> Config:
    path = path.resolve()
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (copy config.example.toml to start)") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None

    base = path.parent

    def resolve(p: str) -> Path:
        p = Path(p).expanduser()
        return p if p.is_absolute() else (base / p).resolve()

    library = raw.get("library", {})
    app = raw.get("app", {})
    if not library.get("path"):
        raise ConfigError("[library] path is required")

    sources: list[Source] = []
    for s in raw.get("source", []):
        name, kind = s.get("name"), s.get("type")
        if not name:
            raise ConfigError("every [[source]] needs a name")
        if any(existing.name == name for existing in sources):
            raise ConfigError(f"duplicate source name: {name}")
        if kind == "rclone":
            if ":" not in s.get("remote", ""):
                raise ConfigError(f"source {name}: remote must look like 'gdrive:' or 'gdrive:Photos'")
            location = s["remote"]
        elif kind == "folder":
            if not s.get("path"):
                raise ConfigError(f"source {name}: path is required")
            location = str(resolve(s["path"]))
        else:
            raise ConfigError(f"source {name}: type must be 'rclone' or 'folder', not {kind!r}")
        sources.append(Source(name, kind, location, bool(s.get("enabled", True))))
    if not sources:
        raise ConfigError("no [[source]] entries in config")

    extensions = app.get("extensions")
    batch_size = int(app.get("batch_size", 200))
    if batch_size < 1:
        raise ConfigError("[app] batch_size must be at least 1")

    return Config(
        library=resolve(library["path"]),
        folder_pattern=library.get("folder_pattern", "%Y/%m"),
        state_dir=resolve(app.get("state_dir", "state")),
        staging_dir=resolve(app.get("staging_dir", "staging")),
        rclone=app.get("rclone", "rclone"),
        batch_size=batch_size,
        extensions=frozenset(e.lower().lstrip(".") for e in extensions) if extensions else DEFAULT_EXTENSIONS,
        sources=tuple(sources),
    )
