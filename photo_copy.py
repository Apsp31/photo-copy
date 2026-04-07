"""
Photo Organizer (v5)

Refactored GUI from tkinter (v4.2) to PySide6.

Key changes from v4.2:
- PySide6 replaces tkinter; tkcalendar dependency removed entirely
- QThread + Signal/Slot replaces all root.after() workarounds for thread safety
- Signals emitted from worker threads are automatically queued to the main thread
- QDateEdit provides a native calendar date picker without third-party packages
- QStatusBar, QProgressBar, QTreeWidget for a cleaner modern UI
- Business logic (date parsing, EXIF, hashing, copying) extracted to module-level
  functions so they can be tested independently of the GUI

Requirements
- Python 3.10+, PySide6, Pillow  (pip install PySide6 Pillow)
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from time import time

from PIL import Image

from PySide6.QtCore import QByteArray, QDate, QObject, Qt, QThread, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

APP_VERSION = "5.3.0"
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# File-type constants
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts",
    ".3gp", ".wmv", ".webm",
})
MEDIA_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".heic", ".raw", ".cr2", ".nef", ".dng", ".arw", ".orf",
}) | VIDEO_EXTENSIONS

# EXIF tag IDs (checked in priority order)
_EXIF_DATETIME_ORIGINAL  = 36867  # DateTimeOriginal
_EXIF_DATETIME_DIGITIZED = 36868  # DateTimeDigitized
_EXIF_DATETIME           = 306    # DateTime

# Regex patterns for date extraction from filenames
_RE_DATE_DASHED  = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_RE_DATE_COMPACT = re.compile(r"(?<!\d)(\d{8})(?!\d)")


# ---------------------------------------------------------------------------
# Framework-agnostic business logic
# ---------------------------------------------------------------------------

def get_exif_date(file_path: str) -> datetime | None:
    """Return the capture datetime from EXIF, or None if unavailable."""
    try:
        with Image.open(file_path) as image:
            try:
                exif_data = image.getexif()          # Pillow 8.2+ public API
            except AttributeError:
                exif_data = image._getexif() or {}   # fallback for older Pillow
            for tag_id in (_EXIF_DATETIME_ORIGINAL, _EXIF_DATETIME_DIGITIZED, _EXIF_DATETIME):
                raw = exif_data.get(tag_id)
                if raw:
                    try:
                        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        log.warning("Malformed EXIF date '%s' in %s", raw, file_path)
    except Exception:
        pass
    return None


def get_file_date(file_path: str) -> datetime | None:
    """
    Return a capture date for the file using (in priority order):
      1. YYYY-MM-DD pattern in filename stem
      2. YYYYMMDD pattern in filename stem
      3. EXIF data (images only; videos go to Undated/ if no filename date)
    Returns None if no reliable date is found.
    mtime is intentionally excluded: it changes on copy and cannot be trusted.
    """
    stem = os.path.splitext(os.path.basename(file_path))[0]

    for match in _RE_DATE_DASHED.finditer(stem):
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d")
        except ValueError:
            pass

    for match in _RE_DATE_COMPACT.finditer(stem):
        try:
            return datetime.strptime(match.group(1), "%Y%m%d")
        except ValueError:
            pass

    if os.path.splitext(file_path)[1].lower() not in VIDEO_EXTENSIONS:
        return get_exif_date(file_path)

    return None


def files_are_identical(f1: str, f2: str) -> bool:
    """
    True if f1 and f2 have identical content.
    Uses a staged approach: size → mtime → full read (small) → partial MD5 (large).
    """
    try:
        s1, s2 = os.stat(f1), os.stat(f2)
        if s1.st_size != s2.st_size:
            return False
        if s1.st_mtime == s2.st_mtime:
            return True
        if s1.st_size < 8192:
            with open(f1, "rb") as a, open(f2, "rb") as b:
                return a.read() == b.read()

        BLOCK = 2 * 1024 * 1024

        def partial_hash(path: str, size: int) -> bytes:
            h = hashlib.md5()
            with open(path, "rb") as fh:
                h.update(fh.read(BLOCK))
                if size > BLOCK * 3:
                    fh.seek(size // 2)
                    h.update(fh.read(BLOCK))
                    fh.seek(-BLOCK, 2)
                    h.update(fh.read(BLOCK))
            return h.digest()

        return partial_hash(f1, s1.st_size) == partial_hash(f2, s1.st_size)
    except (OSError, IOError):
        return False


# ---------------------------------------------------------------------------
# Worker: Scan
# ---------------------------------------------------------------------------

class ScanWorker(QObject):
    """
    Scans source recursively for media files and determines destination paths.
    Runs in a QThread; communicates with the UI exclusively via signals.
    """
    status_changed   = Signal(str)
    progress_changed = Signal(int)
    batch_ready      = Signal(list)   # list of (src_path, dst_path) tuples
    finished         = Signal()

    def __init__(
        self,
        source: str,
        dest: str,
        start_date,        # datetime.date
        end_date,          # datetime.date
        dir_cache: dict,
    ):
        super().__init__()
        self.source     = source
        self.dest       = dest
        self.start_date = start_date
        self.end_date   = end_date
        self._dir_cache = dir_cache
        self._cancel    = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _find_matching_folder(self, date: datetime) -> str | None:
        year_path = os.path.join(self.dest, str(date.year))
        if not os.path.exists(year_path):
            return None
        if year_path not in self._dir_cache:
            try:
                self._dir_cache[year_path] = [
                    e.name for e in os.scandir(year_path) if e.is_dir()
                ]
            except OSError:
                return None
        prefix = date.strftime("%Y-%m-%d")
        for entry in self._dir_cache[year_path]:
            if entry.startswith(prefix):
                return os.path.join(year_path, entry)
        return None

    def run(self) -> None:
        start_time    = time()
        processed     = 0
        found_files   = 0
        scanned_count = 0

        def scan_dir(path: str):
            try:
                with os.scandir(path) as it:
                    files, dirs = [], []
                    for e in it:
                        if e.is_file():
                            files.append(e.name)
                        elif e.is_dir():
                            dirs.append(e.path)
                    return files, dirs
            except OSError:
                return [], []

        dirs_to_scan = [self.source]
        batch: list  = []

        while dirs_to_scan:
            if self._cancel.is_set():
                self.status_changed.emit("Operation cancelled.")
                self.finished.emit()
                return

            current_dir    = dirs_to_scan.pop()
            files, subdirs = scan_dir(current_dir)
            dirs_to_scan.extend(subdirs)
            found_files += len(files)

            for file in files:
                if self._cancel.is_set():
                    self.status_changed.emit("Operation cancelled.")
                    self.finished.emit()
                    return

                scanned_count += 1
                if os.path.splitext(file)[1].lower() not in MEDIA_EXTENSIONS:
                    continue

                fp   = os.path.normpath(os.path.join(current_dir, file))
                date = get_file_date(fp)

                if date and not (self.start_date <= date.date() <= self.end_date):
                    continue

                if date:
                    year_folder = os.path.normpath(os.path.join(self.dest, str(date.year)))
                    date_folder = self._find_matching_folder(date) or os.path.normpath(
                        os.path.join(year_folder, date.strftime("%Y-%m-%d"))
                    )
                else:
                    date_folder = os.path.normpath(os.path.join(self.dest, "Undated"))

                dst = os.path.normpath(os.path.join(date_folder, file))

                if os.path.exists(dst):
                    try:
                        if (
                            os.path.getsize(fp) == os.path.getsize(dst)
                            and files_are_identical(fp, dst)
                        ):
                            continue
                        base, ext = os.path.splitext(dst)
                        n = 1
                        while os.path.exists(dst):
                            dst = os.path.normpath(f"{base}_{n}{ext}")
                            n += 1
                    except OSError:
                        pass

                batch.append((fp, dst))
                processed += 1

                if len(batch) >= 50:
                    self.batch_ready.emit(list(batch))
                    batch.clear()
                    self.progress_changed.emit(
                        int(scanned_count / max(found_files, 1) * 100)
                    )
                    self.status_changed.emit(
                        f"Scanning… {scanned_count}/{found_files} files · "
                        f"{processed} to copy"
                    )

            if found_files > 0 and found_files % 500 == 0:
                self.status_changed.emit(
                    f"Found {found_files} files, {processed} to copy…"
                )

        if batch:
            self.batch_ready.emit(list(batch))

        elapsed = time() - start_time
        self.status_changed.emit(
            f"Scan complete — {scanned_count} files scanned, "
            f"{processed} to copy ({elapsed:.1f}s)"
        )
        self.finished.emit()


# ---------------------------------------------------------------------------
# Worker: Copy
# ---------------------------------------------------------------------------

class CopyWorker(QObject):
    """
    Copies a list of (src, dst) pairs using a thread pool.
    Runs in a QThread; communicates with the UI exclusively via signals.
    """
    status_changed   = Signal(str)
    progress_changed = Signal(int)
    fatal_error      = Signal(str, str)            # title, message
    finished         = Signal(int, float, object, bool)  # copied, elapsed, audit_log, cancelled

    def __init__(self, files_to_copy: list[tuple[str, str]], dest: str):
        super().__init__()
        self.files_to_copy = files_to_copy
        self.dest          = dest
        self._cancel       = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @staticmethod
    def _copy_one(src: str, dst: str) -> None:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    def run(self) -> None:
        total       = len(self.files_to_copy)
        audit_log: dict[str, list[str]] = {}
        copied      = 0
        start_time  = time()
        max_workers = min(10, os.cpu_count() or 4)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(self._copy_one, src, dst): (src, dst)
                for src, dst in self.files_to_copy
            }
            for future in concurrent.futures.as_completed(futures):
                if self._cancel.is_set():
                    self.status_changed.emit("Operation cancelled.")
                    self.finished.emit(copied, time() - start_time, audit_log, True)
                    return

                src, dst = futures[future]
                try:
                    future.result()
                    copied += 1
                    audit_log.setdefault(os.path.dirname(dst), []).append(
                        os.path.basename(dst)
                    )
                    if copied % 5 == 0 or copied == total:
                        self.progress_changed.emit(int(copied / total * 100))
                    self.status_changed.emit(
                        f"Copying {os.path.basename(src)} ({copied}/{total})"
                    )
                except FileNotFoundError:
                    log.warning("Source not found, skipped: %s", src)
                except PermissionError as e:
                    log.error("Permission denied: %s — %s", src, e)
                    self.fatal_error.emit(
                        "Permission Error",
                        f"Permission denied:\n{src}\n\nCopy stopped.",
                    )
                    self.finished.emit(copied, time() - start_time, audit_log, False)
                    return
                except OSError as e:
                    log.error("OS error copying %s: %s", src, e)
                    self.fatal_error.emit(
                        "Copy Error",
                        f"Failed to copy:\n{src}\n\n{e}\n\nCopy stopped.",
                    )
                    self.finished.emit(copied, time() - start_time, audit_log, False)
                    return
                except Exception as e:
                    log.error("Unexpected error copying %s: %s", src, e)

        self.finished.emit(copied, time() - start_time, audit_log, False)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class PhotoOrganizer(QMainWindow):
    CONFIG_FILE = os.path.join(_SCRIPT_DIR, "config.json")

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Photo Organizer v{APP_VERSION}")
        self.resize(960, 640)

        self._files_to_copy: list[tuple[str, str]] = []
        self._dir_cache:     dict                  = {}
        self._scan_thread:   QThread | None        = None
        self._copy_thread:   QThread | None        = None
        self._scan_worker:   ScanWorker | None     = None
        self._copy_worker:   CopyWorker | None     = None
        self._profiles:      dict                  = {}   # name → {source, dest, start, end}
        self._loading_profile = False               # guard against recursive saves

        self._build_ui()
        self._load_config()

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setSpacing(6)
        vbox.setContentsMargins(10, 10, 10, 6)

        # Profile selector row
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("Profile:"))
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(220)
        self._profile_combo.currentTextChanged.connect(self._on_profile_selected)
        profile_row.addWidget(self._profile_combo)
        save_profile_btn = QPushButton("Save Profile")
        save_profile_btn.clicked.connect(self._save_profile)
        profile_row.addWidget(save_profile_btn)
        delete_profile_btn = QPushButton("Delete Profile")
        delete_profile_btn.clicked.connect(self._delete_profile)
        profile_row.addWidget(delete_profile_btn)
        profile_row.addStretch()
        vbox.addLayout(profile_row)

        # Source / dest folder rows
        self._src_edit = QLineEdit()
        self._dst_edit = QLineEdit()
        vbox.addLayout(self._folder_row("Source Folder:", self._src_edit, self._browse_source))
        vbox.addLayout(self._folder_row("Destination Folder:", self._dst_edit, self._browse_dest))

        # Date filter row
        date_row = QHBoxLayout()
        date_row.addWidget(QLabel("Start Date:"))
        self._start_date = QDateEdit(QDate.currentDate())
        self._start_date.setCalendarPopup(True)
        self._start_date.setDisplayFormat("yyyy-MM-dd")
        date_row.addWidget(self._start_date)
        date_row.addSpacing(20)
        date_row.addWidget(QLabel("End Date:"))
        self._end_date = QDateEdit(QDate.currentDate())
        self._end_date.setCalendarPopup(True)
        self._end_date.setDisplayFormat("yyyy-MM-dd")
        date_row.addWidget(self._end_date)
        date_row.addStretch()
        vbox.addLayout(date_row)

        # Action buttons
        btn_row = QHBoxLayout()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_operation)
        btn_row.addWidget(self._cancel_btn)

        self._scan_btn = QPushButton("Scan for Files")
        self._scan_btn.clicked.connect(self._scan_files)
        btn_row.addWidget(self._scan_btn)

        self._copy_btn = QPushButton("Copy Files")
        self._copy_btn.clicked.connect(self._copy_files)
        btn_row.addWidget(self._copy_btn)

        open_btn = QPushButton("Open Destination")
        open_btn.clicked.connect(self._open_destination)
        btn_row.addWidget(open_btn)
        vbox.addLayout(btn_row)

        # File list (two-column tree)
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Source File", "Target Path"])
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._tree_context_menu)
        vbox.addWidget(self._tree)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setValue(0)
        vbox.addWidget(self._progress)

        # Status bar — message on left, version label permanently on right
        self.statusBar().showMessage("Ready.")
        version_label = QLabel(f"v{APP_VERSION}")
        version_label.setStyleSheet("color: grey; padding-right: 6px;")
        self.statusBar().addPermanentWidget(version_label)

    @staticmethod
    def _folder_row(label: str, edit: QLineEdit, browse_fn) -> QHBoxLayout:
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(130)
        row.addWidget(lbl)
        row.addWidget(edit)
        btn = QPushButton("Browse…")
        btn.setFixedWidth(80)
        btn.clicked.connect(browse_fn)
        row.addWidget(btn)
        return row

    # ── Context menu ─────────────────────────────────────────────────

    def _tree_context_menu(self, pos) -> None:
        if not self._tree.selectedItems():
            return
        menu = QMenu(self)
        action = QAction("Remove from list", self)
        action.triggered.connect(self._remove_selected)
        menu.addAction(action)
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _remove_selected(self) -> None:
        selected = self._tree.selectedItems()
        srcs     = {item.text(0) for item in selected}
        root     = self._tree.invisibleRootItem()
        for item in selected:
            root.removeChild(item)
        self._files_to_copy = [(s, d) for s, d in self._files_to_copy if s not in srcs]
        self.statusBar().showMessage(f"{len(self._files_to_copy)} files ready to copy.")

    # ── Open destination ─────────────────────────────────────────────

    def _open_destination(self) -> None:
        dst = self._dst_edit.text().strip()
        if not dst or not os.path.isdir(dst):
            QMessageBox.warning(self, "No Destination",
                                "Please select a valid destination folder first.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(dst)
            elif sys.platform == "darwin":
                subprocess.run(["open", dst])
            else:
                subprocess.run(["xdg-open", dst])
        except Exception as e:
            log.error("Could not open destination: %s", e)

    # ── Profiles ─────────────────────────────────────────────────────

    @staticmethod
    def _suggest_profile_name(source_path: str) -> str:
        """
        Derive a sensible profile name from the source path.
        Walks up the path components looking for something that looks like a
        phone/device name (e.g. 'iPhone 14', 'Galaxy S23', 'Pixel 7').
        Falls back to the last non-empty path component.
        """
        parts = [p for p in re.split(r"[\\/]", source_path) if p]
        _device_hints = re.compile(
            r"(iphone|ipad|galaxy|pixel|samsung|huawei|oneplus|xiaomi|oppo|sony|nokia|lg|moto)",
            re.IGNORECASE,
        )
        for part in reversed(parts):
            if _device_hints.search(part):
                return part
        return parts[-1] if parts else "New Profile"

    def _populate_profile_combo(self, select_name: str = "") -> None:
        """Rebuild the combo from self._profiles, optionally selecting select_name."""
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        self._profile_combo.addItem("")          # blank = no profile loaded
        for name in sorted(self._profiles):
            self._profile_combo.addItem(name)
        if select_name:
            idx = self._profile_combo.findText(select_name)
            if idx >= 0:
                self._profile_combo.setCurrentIndex(idx)
        self._profile_combo.blockSignals(False)

    def _on_profile_selected(self, name: str) -> None:
        if not name or self._loading_profile:
            return
        profile = self._profiles.get(name)
        if not profile:
            return
        self._loading_profile = True
        try:
            self._src_edit.setText(profile.get("source_folder", ""))
            self._dst_edit.setText(profile.get("dest_root_folder", ""))
            for key, picker in (("start_date", self._start_date), ("end_date", self._end_date)):
                val = profile.get(key, "")
                if val:
                    try:
                        d = datetime.strptime(val, "%Y-%m-%d").date()
                        picker.setDate(QDate(d.year, d.month, d.day))
                    except ValueError:
                        pass
        finally:
            self._loading_profile = False

    def _save_profile(self) -> None:
        """Save current fields as a named profile, auto-suggesting a name."""
        suggested = self._suggest_profile_name(self._src_edit.text().strip())
        # If a profile is already selected, default to updating it
        current = self._profile_combo.currentText()
        if current:
            suggested = current

        name, ok = QInputDialog.getText(
            self, "Save Profile", "Profile name:", text=suggested
        )
        if not ok or not name.strip():
            return
        name = name.strip()

        self._profiles[name] = {
            "source_folder":    self._src_edit.text().strip(),
            "dest_root_folder": self._dst_edit.text().strip(),
            "start_date":       self._start_date.date().toString("yyyy-MM-dd"),
            "end_date":         self._end_date.date().toString("yyyy-MM-dd"),
        }
        self._populate_profile_combo(select_name=name)
        self._save_config()
        self.statusBar().showMessage(f"Profile '{name}' saved.")

    def _delete_profile(self) -> None:
        name = self._profile_combo.currentText()
        if not name:
            QMessageBox.information(self, "No Profile Selected",
                                    "Select a profile from the dropdown first.")
            return
        reply = QMessageBox.question(
            self, "Delete Profile",
            f"Delete profile '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._profiles.pop(name, None)
            self._populate_profile_combo()
            self._save_config()
            self.statusBar().showMessage(f"Profile '{name}' deleted.")

    # ── Config ───────────────────────────────────────────────────────

    def _load_config(self) -> None:
        if not os.path.exists(self.CONFIG_FILE):
            return
        try:
            with open(self.CONFIG_FILE) as f:
                cfg = json.load(f)

            # Restore profiles first so the combo is populated before we set fields
            self._profiles = cfg.get("profiles", {})
            last_profile   = cfg.get("last_profile", "")
            self._populate_profile_combo(select_name=last_profile)

            # Restore last-used fields (may be overridden by profile selection above)
            self._src_edit.setText(cfg.get("source_folder", ""))
            self._dst_edit.setText(cfg.get("dest_root_folder", ""))
            for key, picker in (("start_date", self._start_date), ("end_date", self._end_date)):
                val = cfg.get(key, "")
                if val:
                    try:
                        d = datetime.strptime(val, "%Y-%m-%d").date()
                        picker.setDate(QDate(d.year, d.month, d.day))
                    except ValueError:
                        pass

            geo = cfg.get("geometry", "")
            if geo:
                try:
                    self.restoreGeometry(QByteArray.fromHex(geo.encode()))
                except Exception:
                    pass
        except Exception as e:
            log.error("Error loading config: %s", e)

    def _save_config(self) -> None:
        if self._loading_profile:
            return
        cfg = {
            "source_folder":    self._src_edit.text().strip(),
            "dest_root_folder": self._dst_edit.text().strip(),
            "start_date":       self._start_date.date().toString("yyyy-MM-dd"),
            "end_date":         self._end_date.date().toString("yyyy-MM-dd"),
            "geometry":         self.saveGeometry().toHex().data().decode(),
            "profiles":         self._profiles,
            "last_profile":     self._profile_combo.currentText(),
        }
        try:
            with open(self.CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            log.error("Error saving config: %s", e)

    def closeEvent(self, event) -> None:
        self._save_config()
        super().closeEvent(event)

    # ── Browse ───────────────────────────────────────────────────────

    def _browse_source(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Source Folder")
        if path:
            self._src_edit.setText(os.path.normpath(path))
            # Auto-suggest a profile name in the combo if no profile is active
            if not self._profile_combo.currentText():
                suggested = self._suggest_profile_name(path)
                # Just a hint — user still has to click Save Profile
                self.statusBar().showMessage(
                    f"Tip: click 'Save Profile' to save this as '{suggested}'"
                )
            self._save_config()

    def _browse_dest(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Destination Folder")
        if path:
            self._dst_edit.setText(os.path.normpath(path))
            self._save_config()

    # ── Validation ───────────────────────────────────────────────────

    def _validate_paths(self) -> bool:
        src = self._src_edit.text().strip()
        dst = self._dst_edit.text().strip()
        if not os.path.isdir(src):
            QMessageBox.critical(self, "Invalid Source",
                                 "The selected source folder does not exist.")
            return False
        if not os.path.isdir(dst):
            QMessageBox.critical(self, "Invalid Destination",
                                 "The selected destination folder does not exist.")
            return False
        try:
            if os.path.samefile(src, dst):
                QMessageBox.critical(self, "Invalid Paths",
                                     "Source and destination cannot be the same folder.")
                return False
        except OSError:
            pass
        return True

    def _validate_dates(self) -> bool:
        if self._start_date.date() > self._end_date.date():
            QMessageBox.critical(self, "Invalid Date Range",
                                 "Start date must be on or before end date.")
            return False
        return True

    # ── Button state ─────────────────────────────────────────────────

    def _set_busy(self, busy: bool) -> None:
        self._cancel_btn.setEnabled(busy)
        self._scan_btn.setEnabled(not busy)
        self._copy_btn.setEnabled(not busy)

    # ── Scan ─────────────────────────────────────────────────────────

    def _scan_files(self) -> None:
        if not self._validate_paths() or not self._validate_dates():
            return
        self._save_config()

        src   = self._src_edit.text().strip()
        dst   = self._dst_edit.text().strip()
        qsd   = self._start_date.date()
        qed   = self._end_date.date()
        start = datetime(qsd.year(), qsd.month(), qsd.day()).date()
        end   = datetime(qed.year(), qed.month(), qed.day()).date()

        self._tree.clear()
        self._files_to_copy.clear()
        self._dir_cache = {}
        self._progress.setValue(0)
        self.statusBar().showMessage("Scanning…")
        self._set_busy(True)

        self._scan_thread = QThread()
        self._scan_worker = ScanWorker(src, dst, start, end, self._dir_cache)
        self._scan_worker.moveToThread(self._scan_thread)

        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.finished.connect(self._scan_thread.quit)
        self._scan_worker.finished.connect(self._scan_worker.deleteLater)
        self._scan_thread.finished.connect(self._scan_thread.deleteLater)
        self._scan_thread.finished.connect(lambda: self._set_busy(False))

        self._scan_worker.status_changed.connect(self.statusBar().showMessage)
        self._scan_worker.progress_changed.connect(self._progress.setValue)
        self._scan_worker.batch_ready.connect(self._on_batch_ready)

        self._scan_thread.start()

    def _on_batch_ready(self, batch: list) -> None:
        for src, dst in batch:
            self._files_to_copy.append((src, dst))
            self._tree.addTopLevelItem(QTreeWidgetItem([src, dst]))

    # ── Copy ─────────────────────────────────────────────────────────

    def _copy_files(self) -> None:
        if not self._files_to_copy:
            QMessageBox.information(self, "No Files", "There are no files to copy.")
            return

        dst = self._dst_edit.text().strip()

        # Pre-copy disk space check
        try:
            needed = sum(os.path.getsize(s) for s, _ in self._files_to_copy)
            free   = shutil.disk_usage(dst).free
            if needed > free:
                reply = QMessageBox.question(
                    self, "Low Disk Space",
                    f"Not enough free space on destination.\n"
                    f"Needed:    {needed / 1024**2:.1f} MB\n"
                    f"Available: {free   / 1024**2:.1f} MB\n\n"
                    f"Proceed anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.No:
                    return
        except OSError:
            pass

        self._progress.setValue(0)
        self.statusBar().showMessage("Copying…")
        self._set_busy(True)

        self._copy_thread = QThread()
        self._copy_worker = CopyWorker(list(self._files_to_copy), dst)
        self._copy_worker.moveToThread(self._copy_thread)

        self._copy_thread.started.connect(self._copy_worker.run)
        self._copy_worker.finished.connect(self._copy_thread.quit)
        self._copy_worker.finished.connect(self._copy_worker.deleteLater)
        self._copy_thread.finished.connect(self._copy_thread.deleteLater)
        self._copy_thread.finished.connect(lambda: self._set_busy(False))

        self._copy_worker.status_changed.connect(self.statusBar().showMessage)
        self._copy_worker.progress_changed.connect(self._progress.setValue)
        self._copy_worker.fatal_error.connect(self._on_copy_fatal)
        self._copy_worker.finished.connect(self._on_copy_finished)

        self._copy_thread.start()

    def _on_copy_fatal(self, title: str, msg: str) -> None:
        QMessageBox.critical(self, title, msg)

    def _on_copy_finished(
        self, copied: int, elapsed: float, audit_log: object, cancelled: bool
    ) -> None:
        if cancelled:
            self.statusBar().showMessage(f"Cancelled — {copied} files copied before stopping.")
            return
        self.statusBar().showMessage(f"Copied {copied} files in {elapsed:.2f}s.")
        QMessageBox.information(self, "Done", f"Copied {copied} files.")
        self._write_audit_log(audit_log)  # type: ignore[arg-type]
        self._scan_files()                # auto-rescan to confirm clean state

    # ── Cancel ───────────────────────────────────────────────────────

    def _cancel_operation(self) -> None:
        if self._scan_worker:
            self._scan_worker.cancel()
        if self._copy_worker:
            self._copy_worker.cancel()
        self.statusBar().showMessage("Cancelling…")

    # ── Audit log ────────────────────────────────────────────────────

    def _write_audit_log(self, audit_log: dict[str, list[str]]) -> None:
        if not audit_log:
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path      = os.path.join(
            self._dst_edit.text().strip(), f"audit_log_{timestamp}.txt"
        )
        try:
            with open(path, "w") as f:
                for folder in sorted(audit_log):
                    f.write(f"Folder: {os.path.normpath(folder)}\n")
                    for fname in sorted(audit_log[folder]):
                        f.write(f"  {fname}\n")
                    f.write("\n")
            log.info("Audit log written to %s", path)
        except Exception as e:
            log.error("Error writing audit log: %s", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = PhotoOrganizer()
    window.show()
    sys.exit(app.exec())
