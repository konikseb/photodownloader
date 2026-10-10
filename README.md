# photosync

Copies every photo and video from Google Drive, OneDrive (including Samsung Gallery sync) and local
folders into one library on your NAS, sorted by the date each photo was taken:

```
\\NAS\photo\Library\
  2019\07\beach.jpg
  2023\05\20230512_143022.heic
  _unknown-date\scan.png
```

- **Read-only:** cloud accounts are connected with read-only permissions. Nothing is ever deleted,
  not in the clouds, not on the NAS, not in local source folders.
- **Incremental:** each run only downloads files that are new or changed since the last run.
- **No duplicates:** files are compared by content (SHA-256). A photo that is in both Google Drive and
  OneDrive is stored once. Two different photos with the same name get `beach.jpg`, `beach_1.jpg`.
- **Safe to interrupt:** progress is saved after every file. Stop it any time; the next run continues.

The date folder comes from, in order: the photo's EXIF date, a video's creation date, a date in the
file name (`20230512_143022.jpg`, `PXL_…`, `IMG-20230512-WA0001.jpg`), then the file's modified time.

## Setup (Windows)

Keep this folder somewhere like `C:\PRIV\photosync`, not under `AppData`: the Microsoft Store version
of Python can't see files under `AppData`.

1. Install rclone. It handles the cloud logins and downloads:
   ```
   winget install Rclone.Rclone
   ```
2. Install the Python dependencies (Python 3.11 or newer):
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
3. Connect your clouds by running `rclone config`, then `n` for a new remote. A browser window opens
   for you to sign in.
   - **Google Drive:** name it `gdrive`, storage `drive`, and pick scope **`drive.readonly`**.
     For a large Drive, a [private client ID](https://rclone.org/drive/#making-your-own-client-id)
     makes listing and downloading much faster than rclone's shared one.
   - **OneDrive:** name it `onedrive`, storage `onedrive`. When asked to edit the advanced config,
     answer `y` and set `access_scopes` to the read-only option
     (`Files.Read Files.Read.All Sites.Read.All offline_access`).

   Check that both work:
   ```
   rclone lsd gdrive:
   rclone lsd onedrive:
   ```
4. Copy `config.example.toml` to `config.toml` and set `[library] path` to your NAS share.

## Use

```
.venv\Scripts\python -m photosync run --dry-run     # count what would be copied, download nothing
.venv\Scripts\python -m photosync run --limit 20    # try 20 files per source, then look at the NAS
.venv\Scripts\python -m photosync run               # the real thing; the first run can take hours
.venv\Scripts\python -m photosync status            # what's in the library
```

Add `--source onedrive` to run a single source, and `-v` to log every file. Logs are written to
`state\photosync.log` (this app) and `state\rclone.log` (downloads).

### Run it every night

Run this in PowerShell from this folder. It runs the sync daily at 03:00, while you're logged in:

```powershell
$action = New-ScheduledTaskAction -Execute "$PWD\.venv\Scripts\python.exe" -Argument "-m photosync run" -WorkingDirectory "$PWD"
$trigger = New-ScheduledTaskTrigger -Daily -At 3am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName "photosync" -Action $action -Trigger $trigger -Settings $settings
```

Use a UNC path (`\\NAS\photo\...`) for the library: scheduled tasks often can't see mapped drive letters.

## Sources

| What | How |
| --- | --- |
| Google Drive | `rclone` source, remote `gdrive:` (or `gdrive:Photos` for one folder) |
| OneDrive | `rclone` source, remote `onedrive:` |
| Samsung Galaxy phone | Turn on OneDrive sync in Samsung Gallery's settings; the OneDrive source then picks the photos up. Samsung Cloud has no API. |
| Google Photos | Google's API no longer allows reading your whole library. Export it with [Google Takeout](https://takeout.google.com), unzip it, and add the folder as a `folder` source. |
| Phone over USB, old disks | Copy the files to a normal folder first (a phone over USB isn't a real drive letter), then add it as a `folder` source. |

## Moving it to the NAS later

Only the config paths are Windows-specific. To run it on the NAS, put it in a Python container with
rclone installed, mount the photo share as the library, and copy over `state\` (the index) and your
rclone config (`rclone config file` shows where it is).

## Tests

```
.venv\Scripts\python -m unittest discover tests
```
