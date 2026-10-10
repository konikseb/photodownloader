import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from photosync import config as config_mod
from photosync.library import Library
from photosync.metadata import date_taken, from_filename
from photosync.rclone import RemoteFile, local_path
from photosync.sync import sync


def make_jpeg(path: Path, *, taken: datetime | None = None, color=(255, 0, 0)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if taken:
        exif.get_ifd(0x8769)[36867] = taken.strftime("%Y:%m:%d %H:%M:%S")
    Image.new("RGB", (8, 8), color).save(path, "JPEG", exif=exif)
    return path


class FakeRclone:
    """Serves {remote path: local file} as if it were a cloud drive."""

    def __init__(self, files: dict[str, Path], fail: set[str] = frozenset()):
        self.files = files
        self.fail = set(fail)
        self.batches: list[list[str]] = []

    def list_files(self, remote):
        return [RemoteFile(p, f.stat().st_size,
                           datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"))
                for p, f in self.files.items()]

    def download(self, remote, paths, dest, list_file):
        self.batches.append(list(paths))
        for p in paths:
            if p in self.fail:
                continue
            target = dest / local_path(p)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.files[p], target)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="photosync-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lib = self.tmp / "nas" / "Library"

    def load_config(self, sources_toml: str, batch_size: int = 200) -> tuple[config_mod.Config, Library]:
        cfg_file = self.tmp / "config.toml"
        cfg_file.write_text(
            f"[library]\npath = '{self.lib}'\n\n[app]\nbatch_size = {batch_size}\n\n{sources_toml}", encoding="utf-8")
        cfg = config_mod.load(cfg_file)
        library = Library(cfg.library, cfg.folder_pattern, cfg.state_dir / "photosync.db")
        self.addCleanup(library.close)
        return cfg, library


class Dates(unittest.TestCase):
    def test_filename_patterns(self):
        cases = {
            "20230512_143022.jpg": datetime(2023, 5, 12, 14, 30, 22),
            "IMG_20230512_143022.jpg": datetime(2023, 5, 12, 14, 30, 22),
            "PXL_20230512_143022123.mp4": datetime(2023, 5, 12, 14, 30, 22),
            "Screenshot_20230512-143022_Chrome.jpg": datetime(2023, 5, 12, 14, 30, 22),
            "2023-05-12 14.30.22.jpg": datetime(2023, 5, 12, 14, 30, 22),
            "IMG-20230512-WA0001.jpg": datetime(2023, 5, 12),
            "IMG_1234.JPG": None,
            "1683901822000.jpg": None,
            "20231345_000000.jpg": None,
            "20230230_101010.jpg": None,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(from_filename(name), expected)

    def test_exif_beats_filename_and_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            f = make_jpeg(Path(d, "20200101_000000.jpg"), taken=datetime(2018, 3, 4, 5, 6, 7))
            self.assertEqual(date_taken(f, datetime(2024, 1, 1)), (datetime(2018, 3, 4, 5, 6, 7), "exif"))
            g = make_jpeg(Path(d, "holiday.jpg"))
            self.assertEqual(date_taken(g, datetime(2024, 1, 1)), (datetime(2024, 1, 1), "mtime"))
            self.assertEqual(date_taken(g), (None, "unknown"))

    def test_heic_exif(self):
        try:
            import pillow_heif  # noqa: F401  (registered by photosync.metadata)
        except ImportError:
            self.skipTest("pillow-heif not installed")
        with tempfile.TemporaryDirectory() as d:
            exif = Image.Exif()
            exif.get_ifd(0x8769)[36867] = "2021:06:07 08:09:10"
            Image.new("RGB", (16, 16), (1, 2, 3)).save(Path(d, "x.heic"), "HEIF", exif=exif)
            self.assertEqual(date_taken(Path(d, "x.heic")), (datetime(2021, 6, 7, 8, 9, 10), "exif"))

    @unittest.skipUnless(os.name == "nt", "Windows-only name encoding")
    def test_rclone_windows_names(self):
        self.assertEqual(local_path("Trip: Paris/what?.jpg"), Path("Trip： Paris", "what？.jpg"))
        self.assertEqual(local_path("dir. /a.jpg"), Path("dir.␠", "a.jpg"))
        self.assertEqual(local_path("odd./a.jpg"), Path("odd．", "a.jpg"))


class FolderSource(Base):
    def test_import_dedupe_and_rerun(self):
        src = self.tmp / "phone"
        make_jpeg(src / "DCIM/Camera/beach.jpg", taken=datetime(2019, 7, 4, 12), color=(0, 0, 255))
        make_jpeg(src / "DCIM/Camera/20210102_030405.jpg", color=(0, 255, 0))
        (src / "Saved").mkdir()
        shutil.copy(src / "DCIM/Camera/beach.jpg", src / "Saved/beach copy.jpg")
        (src / "notes.txt").write_text("not a photo")
        make_jpeg(src / "DCIM/Camera/.trashed-1700000000-old.jpg", color=(1, 2, 3))
        make_jpeg(src / "DCIM/.thumbnails/thumb.jpg", color=(4, 5, 6))
        cfg, library = self.load_config(f"[[source]]\nname = 'phone'\ntype = 'folder'\npath = '{src}'\n")

        stats = sync(cfg, library, None)["phone"]
        self.assertEqual((stats.listed, stats.imported, stats.duplicates, stats.failed), (3, 2, 1, 0))
        self.assertTrue((self.lib / "2019/07/beach.jpg").is_file())
        self.assertTrue((self.lib / "2021/01/20210102_030405.jpg").is_file())
        self.assertTrue((src / "DCIM/Camera/beach.jpg").is_file(), "source files must never be removed")

        again = sync(cfg, library, None)["phone"]
        self.assertEqual((again.new, again.imported), (0, 0))

        # A different photo with the same name and date gets a suffix instead of overwriting.
        make_jpeg(src / "Other/beach.jpg", taken=datetime(2019, 7, 4, 13), color=(9, 9, 9))
        third = sync(cfg, library, None)["phone"]
        self.assertEqual((third.new, third.imported), (1, 1))
        self.assertTrue((self.lib / "2019/07/beach_1.jpg").is_file())
        self.assertEqual(library.summary()["total"][0][0], 3)

    def test_dry_run_touches_nothing(self):
        src = self.tmp / "phone"
        make_jpeg(src / "a.jpg")
        cfg, library = self.load_config(f"[[source]]\nname = 'phone'\ntype = 'folder'\npath = '{src}'\n")
        stats = sync(cfg, library, None, dry_run=True)["phone"]
        self.assertEqual((stats.new, stats.imported), (1, 0))
        self.assertFalse(self.lib.exists())


class RcloneSource(Base):
    def test_batches_dedupe_across_sources_and_retry(self):
        content = self.tmp / "content"
        a = make_jpeg(content / "a.jpg", taken=datetime(2022, 8, 1), color=(10, 0, 0))
        b = make_jpeg(content / "b.jpg", taken=datetime(2022, 9, 1), color=(20, 0, 0))
        c = make_jpeg(content / "c.jpg", taken=datetime(2023, 1, 1), color=(30, 0, 0))
        drive = FakeRclone({"Photos/Trip: Paris/a.jpg": a, "Photos/b.jpg": b, "Photos/c.jpg": c,
                            "Docs/report.pdf": a}, fail={"Photos/c.jpg"})
        cfg, library = self.load_config(
            "[[source]]\nname = 'gdrive'\ntype = 'rclone'\nremote = 'gdrive:'\n\n"
            "[[source]]\nname = 'onedrive'\ntype = 'rclone'\nremote = 'onedrive:'\n", batch_size=2)

        stats = sync(cfg, library, drive, only=["gdrive"])["gdrive"]
        self.assertEqual((stats.listed, stats.imported, stats.failed), (3, 2, 1))
        self.assertEqual([len(batch) for batch in drive.batches], [2, 1])
        self.assertTrue((self.lib / "2022/08/a.jpg").is_file())
        self.assertFalse(any((cfg.staging_dir).rglob("*.jpg")), "staging must be emptied")

        drive.fail.clear()
        retry = sync(cfg, library, drive, only=["gdrive"])["gdrive"]
        self.assertEqual((retry.new, retry.imported), (1, 1), "only the failed file is fetched again")

        # The same photos synced to a second cloud (e.g. Samsung Gallery -> OneDrive) are not stored twice.
        onedrive = FakeRclone({"Pictures/Samsung Gallery/a.jpg": a, "Pictures/b.jpg": b})
        other = sync(cfg, library, onedrive, only=["onedrive"])["onedrive"]
        self.assertEqual((other.imported, other.duplicates), (0, 2))
        self.assertEqual(library.summary()["total"][0][0], 3)


if __name__ == "__main__":
    unittest.main()
