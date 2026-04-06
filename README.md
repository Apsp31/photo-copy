# photo-copy

Desktop GUI tool to scan a source folder and copy photos and videos into a destination library organised by capture date.

## Features

- Browse source and destination folders
- Filter by start/end date
- Extracts dates from filenames (`YYYY-MM-DD` or `YYYYMMDD`, anywhere in the name) and EXIF data for images
- Undated files (including videos with no filename date) are placed in `Undated/`
- Skips duplicates using size + mtime fast-path, full read for small files, and partial MD5 for large files
- Multi-threaded copying with live progress bar and cancel support
- Writes an audit log to the destination root after each copy run
- Persists source/destination paths and date filters between sessions (`config.json` next to the script)

## Output structure

```
<destination>/
  2023/
    2023-06-15/
      IMG_001.jpg
      IMG_002.heic
    2023-06-20 Holiday/      ← existing folders with a matching date prefix are reused
      VID_001.mp4
  Undated/
    scan001.jpg
```

## Supported formats

**Images:** `.jpg` `.jpeg` `.png` `.gif` `.bmp` `.tiff` `.tif` `.heic` `.raw` `.cr2` `.nef` `.dng` `.arw` `.orf`

**Videos:** `.mp4` `.mov` `.m4v` `.avi` `.mkv` `.mts` `.m2ts` `.3gp` `.wmv` `.webm`

## Requirements

- Python 3.8+
- [Pillow](https://pypi.org/project/Pillow/)
- [tkcalendar](https://pypi.org/project/tkcalendar/)
- A desktop environment with tkinter support

## Installation

```bash
pip install Pillow tkcalendar
```

## Usage

```bash
python photo_copy.py
```

1. Select a **Source Folder** (scanned recursively)
2. Select a **Destination Root Folder**
3. Set **Start Date** / **End Date** to filter by capture date (files outside this range are skipped)
4. Click **Scan for Files** — the table shows what will be copied and where
5. Click **Copy Files** to begin; use **Cancel** to stop mid-run

## Duplicate detection

Files already present at the destination are compared in three stages (fastest to most thorough):

1. **Size mismatch** → not a duplicate, copy
2. **Same size + same mtime** → duplicate, skip
3. **Same size, small file (< 8 KB)** → full byte compare
4. **Same size, large file** → partial MD5 (first 2 MB + middle 2 MB + last 2 MB)

If a file at the destination has the same name but is a different file, it is renamed with a `_1`, `_2`, … suffix.

## Version history

| Version | Notes |
|---------|-------|
| v4.1 | Fixed UI thread safety, config path, undated video handling, cancel race, bare excepts |
| v4 | Added video format support and mtime fallback (later removed in v4.1) |
| v3 | GUI overhaul |
| v2 | Duplicate detection improvements |
| v1 | Initial release |
