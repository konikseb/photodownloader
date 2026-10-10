# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`photosync`: a Python CLI that copies every photo/video from Google Drive, OneDrive and local folders into one
date-sorted, deduplicated library on a NAS (`<library>/YYYY/MM/<name>`). It runs on the user's Windows PC for now;
running it in Docker on the NAS is the intended later step, so keep code free of Windows-only assumptions outside
the `os.name == "nt"` branches that already exist.

## Commands

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m unittest discover tests                    # all tests
.venv\Scripts\python -m unittest tests.test_photosync.Dates.test_filename_patterns   # one test
.venv\Scripts\python -m photosync -c config.toml run --dry-run      # list only, downloads nothing
.venv\Scripts\python -m photosync run --limit 20 --source onedrive -v
.venv\Scripts\python -m photosync status
```

There is no linter or build step. Python is 3.11+ (uses `tomllib`); the machine has Microsoft Store Python 3.13,
which cannot see files under `AppData` (keep the project and test temp data out of there). rclone is an external
binary (`winget install Rclone.Rclone`), not a Python dependency; the tests don't need it.

## Architecture

Pipeline, one source at a time (`sync.sync`):

1. **List**: `rclone lsjson` for `type = "rclone"` sources, `os.walk` for `type = "folder"` sources. Both produce
   `RemoteFile(path, size, mtime)` with `/`-separated paths relative to the source root.
2. **Filter**: by extension, skipping any hidden path segment (Android `.trashed-*`, `.thumbnails`, macOS `._*`).
3. **Diff**: against the `seen` table in SQLite: `(source, path)` → `(size, mtime)`. Anything unchanged is skipped
   without downloading.
4. **Fetch** (rclone sources only): batches of `batch_size` files go through
   `rclone copy --files-from-raw --no-traverse` into `staging/<source>/`, which is wiped before and after each batch.
   Folder sources are read in place and are never modified.
5. **Import** (`Library.import_file`): SHA-256 → if the hash is already in `media`, it's a duplicate (only `seen` is
   updated) → otherwise `metadata.date_taken` picks the folder → `_place` copies to `.<name>.partial`, then
   `os.replace` to the final name, adding `_1`, `_2`… on name clashes → `media` and `seen` rows are committed together.

Date priority in `metadata.date_taken`: EXIF DateTimeOriginal (Pillow + pillow-heif) → video `creation_date`
(hachoir) → date in filename (regex) → source mtime → `_unknown-date/`. The method used is stored in
`media.date_source`. All three libraries are optional imports; missing ones just skip that step.

State lives in `state_dir` (`photosync.db`, `photosync.log`, `rclone.log`). It should stay on a local disk: SQLite
over SMB is unreliable.

## Invariants to preserve

- **Never delete or modify anything** in cloud sources, folder sources or the library. Remotes are configured
  read-only (Drive scope `drive.readonly`, OneDrive `access_scopes` read-only).
- **Mark a file `seen` only after a successful import.** That is what makes failed downloads retry on the next run
  and lets a run be interrupted at any point.
- **Windows names**: rclone's local backend replaces characters Windows forbids with look-alikes (`:` → `：`,
  trailing space → `␠`, etc.). `rclone.local_path` mirrors that so a staged file can be found from its remote path.
  If you change how downloads are staged, keep the two in sync.
- **Long paths**: `sync._long_path` adds the `\\?\` prefix for Python file operations on staging/source paths.
  Never pass prefixed paths to rclone.
- An `OSError` while the library root is unreachable raises `LibraryUnavailable` and aborts the run instead of
  marking every file failed.

## Testing

`sync()` takes the rclone object as a parameter. Tests pass a `FakeRclone` (same `list_files`/`download`
interface) backed by local files, so the batching, retry, staging cleanup and cross-source dedupe paths run
without the rclone binary or cloud accounts. Nothing has been run against real Google Drive/OneDrive yet, and
video date extraction hasn't been tried on real phone videos.

CLI exit codes: `0` ok, `1` some files failed or rclone/library error, `2` config/usage error, `130` interrupted.

## Source-specific context

- Google Photos can't be read through its API since March 2025 (apps only see media they uploaded). The
  supported route is Google Takeout → unzip → `folder` source. Takeout JSON sidecar dates aren't used yet.
- Samsung Cloud has no API. Galaxy photos are expected to arrive through Samsung Gallery's OneDrive sync.
- A phone connected over USB (MTP) is not a filesystem path; files must be copied to a real folder first.
