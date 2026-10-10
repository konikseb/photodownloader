"""Work out when a photo or video was taken."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None  # only headers are read, pixels are never decoded
except ImportError:
    Image = None

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

try:
    from hachoir.core import config as hachoir_config
    from hachoir.metadata import extractMetadata
    from hachoir.parser import createParser
    hachoir_config.quiet = True
except ImportError:
    createParser = None

VIDEO_EXTENSIONS = frozenset({"mp4", "mov", "m4v", "3gp", "avi", "mkv", "mts", "m2ts", "wmv", "webm"})

_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 36867
_DATETIME_DIGITIZED = 36868
_DATETIME = 306

_EXIF_DATE = re.compile(r"(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
# Phone and camera names: 20230512_143022.jpg, IMG_20230512_143022.jpg, PXL_20230512_143022123.mp4,
# Screenshot_20230512-143022_Chrome.jpg, 2023-05-12 14.30.22.jpg, IMG-20230512-WA0001.jpg (date only)
_DATE = r"(?<!\d)((?:19|20)\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
_FILENAME_DATETIME = re.compile(_DATE + r"[-_ .T]?([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d)")
_FILENAME_DATE = re.compile(_DATE + r"(?!\d)")


def date_taken(path: Path, fallback: datetime | None = None) -> tuple[datetime | None, str]:
    """Return (when, how), where how is 'exif', 'video', 'filename', 'mtime' or 'unknown'."""
    is_video = path.suffix.lower().lstrip(".") in VIDEO_EXTENSIONS
    when = _from_video(path) if is_video else _from_exif(path)
    if when:
        return when, "video" if is_video else "exif"
    when = from_filename(path.name)
    if when:
        return when, "filename"
    if fallback and _plausible(fallback):
        return fallback, "mtime"
    return None, "unknown"


def from_filename(name: str) -> datetime | None:
    for pattern in (_FILENAME_DATETIME, _FILENAME_DATE):
        for m in pattern.finditer(name):
            try:
                when = datetime(*map(int, m.groups()))
            except ValueError:  # e.g. Feb 30
                continue
            if _plausible(when):
                return when
    return None


def _plausible(when: datetime) -> bool:
    return datetime(1900, 1, 1) < when.replace(tzinfo=None) < datetime.now() + timedelta(days=2)


def _from_exif(path: Path) -> datetime | None:
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            sub = exif.get_ifd(_EXIF_IFD)
            candidates = [sub.get(_DATETIME_ORIGINAL), sub.get(_DATETIME_DIGITIZED), exif.get(_DATETIME)]
    except Exception as e:  # RAW formats, corrupt files, etc. fall through to the other methods
        log.debug("no EXIF from %s: %s", path, e)
        return None
    for value in candidates:
        if isinstance(value, bytes):
            value = value.decode("ascii", "ignore")
        m = _EXIF_DATE.search(value) if isinstance(value, str) else None
        if not m:
            continue
        try:
            when = datetime(*map(int, m.groups()))
        except ValueError:  # "0000:00:00 00:00:00" and friends
            continue
        if _plausible(when):
            return when
    return None


def _from_video(path: Path) -> datetime | None:
    if createParser is None:
        return None
    try:
        parser = createParser(str(path))
        if parser is None:
            return None
        with parser:
            meta = extractMetadata(parser)
    except Exception as e:
        log.debug("no video metadata from %s: %s", path, e)
        return None
    if not meta or not meta.has("creation_date"):
        return None
    when = meta.get("creation_date")
    # QuickTime/MP4 store 1904-01-01 (or the Unix epoch) when the camera didn't set a date.
    if isinstance(when, datetime) and when.year > 1970 and _plausible(when):
        return when.replace(tzinfo=None)
    return None
