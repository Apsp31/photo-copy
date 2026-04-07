# Changelog

All notable changes to photo-copy are documented here.
Format: `[version] YYYY-MM-DD — summary`

---

## [5.4.0] 2026-04-07
- Scan stats bar shows: scanned · to copy · duplicates skipped · undated · outside date range · elapsed time
- Auto-advance start date: after each successful copy the active profile's start date is moved to today
- Sync All Profiles button: iterates every saved profile in sequence (scan → copy → advance date → next)
  - Profiles with missing/offline source or destination are skipped with a status message
  - Cancel stops the entire queue
- Refactored scan/copy start into `_start_scan` / `_start_copy` helpers to support Sync All flow
- `_on_scan_thread_finished` / `_on_copy_thread_finished` — prevent premature UI un-busy in Sync All mode

## [5.3.0] 2026-04-07
- Added visible version number to title bar and status bar (permanent right-aligned label)
- Added `APP_VERSION` constant to source; introduced CHANGELOG.md

## [5.2.0] 2026-04-07
- Phone profiles: save/load named configurations (source folder, destination, date range)
- Profile name auto-suggested from source path using device keyword detection
- Profiles persisted in config.json; last-used profile restored on launch
- Status bar hints suggested profile name when browsing a new source folder

## [5.1.0] 2026-04-07
- Fixed PySide6 6.x namespaced enum errors (Qt.ContextMenuPolicy, QHeaderView.ResizeMode, etc.)
- Added missing `Qt` import (caused NameError crash on startup)

## [5.0.0] 2026-04-07
- Full GUI rewrite from tkinter to PySide6
- QThread + Signal/Slot replaces all `root.after()` thread-safety workarounds
- Business logic extracted to module-level functions (testable independently of GUI)
- `tkcalendar` dependency removed; replaced by native `QDateEdit` with calendar popup
- `threading.Event` for cancellation, module-level `MEDIA_EXTENSIONS`/`VIDEO_EXTENSIONS` frozensets

## [4.2.0] 2026-04-07
- 15 reliability, UX and code quality improvements including:
  - Validate start ≤ end date; detect source == destination
  - Handle FileNotFoundError, PermissionError, disk-full OSError during copy
  - Public Pillow `getexif()` API; DateTimeDigitized EXIF fallback
  - Per-file status during copy; Open Destination button
  - Window geometry persistence; right-click remove from list
  - Pre-copy disk space check; logging module; regex date parsing

## [4.1.0] 2026-04-06
- config.json saved next to script (not CWD)
- All tkinter UI updates dispatched via `root.after()` for thread safety
- `copy_files` moved to background thread
- Removed unreliable mtime fallback for videos (undated videos → Undated/)
- `found_files % 500` guard; bare `except` → `except Exception`

## [4.0.0] 2026-04-06
- Initial release of this repository (extracted from Photo_Copy project)
- Video format support (.mp4, .mov, .m4v, .avi, .mkv, .mts, .m2ts, .3gp, .wmv, .webm)
- Multi-threaded copying with progress bar and cancel
- Duplicate detection: size + mtime fast-path, full read (small), partial MD5 (large)
- Audit log written to destination after each copy run
