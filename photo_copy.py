"""
Photo Organizer (v4)

Purpose
- Desktop GUI tool to scan and copy photos and videos from a source folder to a destination library organized by capture date.

Key Features
- Source/destination selection, start/end date filtering (tkcalendar DateEntry)
- Date extraction from filenames (YYYY-MM-DD / YYYYMMDD) and EXIF for images
- Undated files (including videos with no filename date) go to <dest>/Undated
- Duplicate detection (size, mtime, partial MD5 for large files)
- Multi-threaded copying with progress and Cancel support
- Audit log written to destination root, and config.json persistence (next to script)

Output Structure
- Files placed under `<dest>/<year>/<YYYY-MM-DD>`; undated files go to `<dest>/Undated`

Requirements
- Python 3.x, Pillow (PIL), tkcalendar, tkinter-enabled desktop environment

Version Notes (v4)
- Added support for common video formats (.mp4, .mov, .m4v, .avi, .mkv, .mts, .m2ts, .3gp, .wmv, .webm)

Fixes (v4.1)
- config.json now saved next to the script, not the working directory
- All tkinter UI updates from background threads dispatched via root.after() for thread safety
- copy_files now runs in a background thread (no longer freezes the UI)
- Removed unreliable mtime fallback for videos; undated videos now go to Undated/
- Cancel flag checked before recording each completed copy future
- Status update condition guarded against firing when found_files == 0
- Bare except clauses replaced with except Exception
"""

import os
import shutil
import sys
import hashlib
import json
from datetime import datetime
from time import time
from PIL import Image
import threading
import concurrent.futures

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ModuleNotFoundError:
    print("Error: tkinter module is not available in this environment.")
    print("Please install it or run this script in a desktop environment that supports tkinter.")
    sys.exit(1)

from tkcalendar import DateEntry

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class PhotoOrganizer:
    CONFIG_FILE = os.path.join(_SCRIPT_DIR, "config.json")

    def __init__(self, root):
        self.root = root
        self.root.title("Photo Organizer")

        self.source_folder = tk.StringVar()
        self.dest_root_folder = tk.StringVar()
        self.status_text = tk.StringVar(value="Ready.")
        self.cancel_operation = False
        self._dir_cache = {}

        self.setup_gui()
        self.files_to_copy = []
        self.load_config()

    # ------------------------------------------------------------------
    # Thread-safe UI helpers
    # ------------------------------------------------------------------

    def _ui(self, fn):
        """Schedule fn() to run on the main tkinter thread."""
        self.root.after(0, fn)

    def _ui_status(self, msg):
        self._ui(lambda: self.status_text.set(msg))

    def _ui_progress(self, val):
        self._ui(lambda: self.progress.configure(value=val))

    def _ui_button(self, button, state):
        self._ui(lambda b=button, s=state: b.config(state=s))

    def _ui_tree_insert(self, rows):
        def _insert(r=rows):
            for src, dst in r:
                self.tree.insert("", "end", values=(src, dst))
        self._ui(_insert)

    def _ui_tree_clear(self):
        self._ui(lambda: self.tree.delete(*self.tree.get_children()))

    def _ui_show_info(self, title, msg):
        self._ui(lambda t=title, m=msg: messagebox.showinfo(t, m))

    # ------------------------------------------------------------------
    # GUI setup
    # ------------------------------------------------------------------

    def setup_gui(self):
        tk.Label(self.root, text="Source Folder:").grid(row=0, column=0, sticky='e')
        tk.Entry(self.root, textvariable=self.source_folder, width=50).grid(row=0, column=1)
        tk.Button(self.root, text="Browse", command=self.browse_source).grid(row=0, column=2)

        tk.Label(self.root, text="Destination Root Folder:").grid(row=1, column=0, sticky='e')
        tk.Entry(self.root, textvariable=self.dest_root_folder, width=50).grid(row=1, column=1)
        tk.Button(self.root, text="Browse", command=self.browse_dest_root).grid(row=1, column=2)

        tk.Button(self.root, text="Scan for Files", command=self.scan_files).grid(row=2, column=1, pady=10)
        tk.Button(self.root, text="Copy Files", command=self.copy_files).grid(row=2, column=2, pady=10)
        self.cancel_button = tk.Button(self.root, text="Cancel", command=self.cancel_current_operation, state=tk.DISABLED)
        self.cancel_button.grid(row=2, column=0, pady=10)

        self.tree = ttk.Treeview(self.root, columns=("Source", "Target"), show='headings')
        self.tree.heading("Source", text="Source File")
        self.tree.heading("Target", text="Target Path")
        self.tree.grid(row=3, column=0, columnspan=3, sticky='nsew')

        self.progress = ttk.Progressbar(self.root, mode='determinate')
        self.progress.grid(row=4, column=0, columnspan=3, sticky='we', pady=(5, 0))

        self.status_label = tk.Label(self.root, textvariable=self.status_text, anchor='w')
        self.status_label.grid(row=5, column=0, columnspan=3, sticky='we', pady=(2, 5))

        tk.Label(self.root, text="Start Date:").grid(row=6, column=0, sticky='e')
        self.start_date_picker = DateEntry(self.root, width=20, date_pattern="yyyy-MM-dd")
        self.start_date_picker.grid(row=6, column=1, sticky='w')

        tk.Label(self.root, text="End Date:").grid(row=7, column=0, sticky='e')
        self.end_date_picker = DateEntry(self.root, width=20, date_pattern="yyyy-MM-dd")
        self.end_date_picker.grid(row=7, column=1, sticky='w')

        self.root.grid_rowconfigure(3, weight=1)
        self.root.grid_columnconfigure(1, weight=1)

    # ------------------------------------------------------------------
    # Config persistence
    # ------------------------------------------------------------------

    def load_config(self):
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    self.source_folder.set(config.get("source_folder", ""))
                    self.dest_root_folder.set(config.get("dest_root_folder", ""))
                    start_date = config.get("start_date", "")
                    end_date = config.get("end_date", "")
                    if start_date:
                        self.start_date_picker.set_date(datetime.strptime(start_date, "%Y-%m-%d").date())
                    if end_date:
                        self.end_date_picker.set_date(datetime.strptime(end_date, "%Y-%m-%d").date())
            except Exception as e:
                print(f"Error loading config: {e}")

    def save_config(self):
        config = {
            "source_folder": self.source_folder.get(),
            "dest_root_folder": self.dest_root_folder.get(),
            "start_date": self.start_date_picker.get_date().strftime("%Y-%m-%d"),
            "end_date": self.end_date_picker.get_date().strftime("%Y-%m-%d"),
        }
        try:
            with open(self.CONFIG_FILE, 'w') as f:
                json.dump(config, f)
        except Exception as e:
            print(f"Error saving config: {e}")

    def browse_source(self):
        self.source_folder.set(os.path.normpath(filedialog.askdirectory()))
        self.save_config()

    def browse_dest_root(self):
        self.dest_root_folder.set(os.path.normpath(filedialog.askdirectory()))
        self.save_config()

    # ------------------------------------------------------------------
    # Date extraction
    # ------------------------------------------------------------------

    def get_exif_date(self, file_path):
        try:
            with Image.open(file_path) as image:
                exif_data = image._getexif()
                if exif_data:
                    date_original = exif_data.get(36867)  # DateTimeOriginal
                    if date_original:
                        return datetime.strptime(date_original, "%Y:%m:%d %H:%M:%S")
                    date_time = exif_data.get(306)  # DateTime
                    if date_time:
                        return datetime.strptime(date_time, "%Y:%m:%d %H:%M:%S")
        except Exception:
            pass
        return None

    def get_file_date(self, file_path):
        filename = os.path.basename(file_path)
        try:
            # YYYY-MM-DD at start
            if len(filename) >= 10 and filename[4] == '-' and filename[7] == '-':
                try:
                    return datetime.strptime(filename[:10], "%Y-%m-%d")
                except ValueError:
                    pass
            # YYYYMMDD at start
            if len(filename) >= 8:
                try:
                    date_part = filename[:8]
                    if date_part.isdigit():
                        return datetime.strptime(date_part, "%Y%m%d")
                except ValueError:
                    pass
            # YYYY-MM-DD anywhere in filename
            for i in range(len(filename) - 9):
                if filename[i + 4] == '-' and filename[i + 7] == '-':
                    try:
                        return datetime.strptime(filename[i:i + 10], "%Y-%m-%d")
                    except ValueError:
                        pass
            # YYYYMMDD anywhere in filename
            for i in range(len(filename) - 7):
                date_part = filename[i:i + 8]
                if date_part.isdigit():
                    try:
                        return datetime.strptime(date_part, "%Y%m%d")
                    except ValueError:
                        pass
        except Exception:
            pass

        # EXIF fallback for images only (not videos)
        if not self.is_video_file(file_path):
            date = self.get_exif_date(file_path)
            if date:
                return date

        # No reliable date found — caller will route to Undated/
        # mtime intentionally not used: it changes on file copy and cannot be trusted
        return None

    # ------------------------------------------------------------------
    # Duplicate detection
    # ------------------------------------------------------------------

    def files_are_identical(self, f1, f2):
        try:
            stat1 = os.stat(f1)
            stat2 = os.stat(f2)
            if stat1.st_size != stat2.st_size:
                return False
            if stat1.st_mtime == stat2.st_mtime and stat1.st_size == stat2.st_size:
                return True
            if stat1.st_size < 8192:
                with open(f1, 'rb') as file1, open(f2, 'rb') as file2:
                    return file1.read() == file2.read()

            def smart_partial_hash(path, file_size, blocksize=2 * 1024 * 1024):
                hasher = hashlib.md5()
                with open(path, 'rb') as afile:
                    hasher.update(afile.read(blocksize))
                    if file_size > blocksize * 3:
                        afile.seek(file_size // 2)
                        hasher.update(afile.read(blocksize))
                        afile.seek(-blocksize, 2)
                        hasher.update(afile.read(blocksize))
                return hasher.digest()

            return smart_partial_hash(f1, stat1.st_size) == smart_partial_hash(f2, stat1.st_size)
        except (OSError, IOError):
            return False

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def validate_paths(self):
        src = self.source_folder.get()
        dst = self.dest_root_folder.get()
        if not os.path.isdir(src):
            messagebox.showerror("Invalid Source", "The selected source folder does not exist.")
            return False
        if not os.path.isdir(dst):
            messagebox.showerror("Invalid Destination", "The selected destination folder does not exist.")
            return False
        return True

    def find_matching_folder(self, date):
        year_path = os.path.join(self.dest_root_folder.get(), str(date.year))
        if not os.path.exists(year_path):
            return None
        if year_path not in self._dir_cache:
            try:
                self._dir_cache[year_path] = [e.name for e in os.scandir(year_path) if e.is_dir()]
            except OSError:
                return None
        prefix = date.strftime("%Y-%m-%d")
        for entry in self._dir_cache[year_path]:
            if entry.startswith(prefix):
                return os.path.join(year_path, entry)
        return None

    # ------------------------------------------------------------------
    # File type checks
    # ------------------------------------------------------------------

    def is_media_file(self, filename):
        if not hasattr(self, '_media_extensions_set'):
            self._media_extensions_set = {
                '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif',
                '.heic', '.raw', '.cr2', '.nef', '.dng', '.arw', '.orf',
                '.mp4', '.mov', '.m4v', '.avi', '.mkv', '.mts', '.m2ts',
                '.3gp', '.wmv', '.webm',
            }
        return os.path.splitext(filename)[1].lower() in self._media_extensions_set

    def is_video_file(self, filename):
        if not hasattr(self, '_video_extensions_set'):
            self._video_extensions_set = {
                '.mp4', '.mov', '.m4v', '.avi', '.mkv', '.mts', '.m2ts',
                '.3gp', '.wmv', '.webm',
            }
        return os.path.splitext(filename)[1].lower() in self._video_extensions_set

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def scan_files(self):
        def scan():
            self.cancel_operation = False
            self._ui_button(self.cancel_button, tk.NORMAL)
            self.save_config()

            if not self.validate_paths():
                self._ui_button(self.cancel_button, tk.DISABLED)
                return

            start_date = self.start_date_picker.get_date()
            end_date = self.end_date_picker.get_date()

            self._ui_tree_clear()
            self.files_to_copy.clear()
            self._ui_progress(0)
            self._ui_status("Scanning...")
            self._dir_cache = {}

            start_time = time()
            processed_files = 0
            found_files = 0
            scanned_count = 0

            def scan_directory(path):
                try:
                    with os.scandir(path) as entries:
                        files, dirs = [], []
                        for entry in entries:
                            if entry.is_file():
                                files.append(entry.name)
                            elif entry.is_dir():
                                dirs.append(entry.path)
                        return files, dirs
                except OSError:
                    return [], []

            dirs_to_scan = [self.source_folder.get()]
            batch_files = []

            while dirs_to_scan:
                if self.cancel_operation:
                    self._ui_status("Operation cancelled.")
                    self._ui_button(self.cancel_button, tk.DISABLED)
                    return

                current_dir = dirs_to_scan.pop()
                files, subdirs = scan_directory(current_dir)
                dirs_to_scan.extend(subdirs)
                found_files += len(files)

                for file in files:
                    if self.cancel_operation:
                        self._ui_status("Operation cancelled.")
                        self._ui_button(self.cancel_button, tk.DISABLED)
                        return

                    scanned_count += 1
                    if not self.is_media_file(file):
                        continue

                    file_path = os.path.normpath(os.path.join(current_dir, file))
                    date = self.get_file_date(file_path)

                    if date and not (start_date <= date.date() <= end_date):
                        continue

                    if date:
                        year_folder = os.path.normpath(os.path.join(self.dest_root_folder.get(), str(date.year)))
                        date_folder = self.find_matching_folder(date)
                        if not date_folder:
                            date_folder = os.path.normpath(os.path.join(year_folder, date.strftime("%Y-%m-%d")))
                    else:
                        date_folder = os.path.normpath(os.path.join(self.dest_root_folder.get(), "Undated"))

                    dest_file_path = os.path.normpath(os.path.join(date_folder, file))

                    if os.path.exists(dest_file_path):
                        try:
                            if (os.path.getsize(file_path) == os.path.getsize(dest_file_path)
                                    and self.files_are_identical(file_path, dest_file_path)):
                                continue
                            base, ext = os.path.splitext(dest_file_path)
                            counter = 1
                            while os.path.exists(dest_file_path):
                                dest_file_path = os.path.normpath(f"{base}_{counter}{ext}")
                                counter += 1
                        except OSError:
                            pass

                    batch_files.append((file_path, dest_file_path))
                    processed_files += 1

                    if len(batch_files) >= 50:
                        rows = list(batch_files)
                        for src, dst in rows:
                            self.files_to_copy.append((src, dst))
                        self._ui_tree_insert(rows)
                        batch_files.clear()
                        self._ui_progress((scanned_count / max(found_files, 1)) * 100)
                        self._ui_status(
                            f"Scanning... {scanned_count}/{found_files} files processed. "
                            f"Found {processed_files} to copy."
                        )

                if found_files > 0 and found_files % 500 == 0:
                    self._ui_status(f"Found {found_files} files, processed {processed_files}")

            # Flush remaining batch
            for src, dst in batch_files:
                self.files_to_copy.append((src, dst))
            self._ui_tree_insert(batch_files)

            elapsed = time() - start_time
            self._ui_status(
                f"Scanned {scanned_count} files. "
                f"{len(self.files_to_copy)} new files ready to copy. "
                f"Time: {elapsed:.2f}s"
            )
            self._ui_button(self.cancel_button, tk.DISABLED)

        threading.Thread(target=scan, daemon=True).start()

    # ------------------------------------------------------------------
    # Copy
    # ------------------------------------------------------------------

    def _copy_single_file(self, src, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return dst

    def copy_files(self):
        if not self.files_to_copy:
            messagebox.showinfo("No Files", "There are no files to copy.")
            return

        def do_copy():
            self.cancel_operation = False
            self._ui_button(self.cancel_button, tk.NORMAL)

            total = len(self.files_to_copy)
            self._ui_progress(0)
            self._ui_status("Copying...")

            start_time = time()
            audit_log = {}
            copied_count = 0
            max_workers = min(10, os.cpu_count() or 4)

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_file = {
                    executor.submit(self._copy_single_file, src, dst): (src, dst)
                    for src, dst in self.files_to_copy
                }
                for future in concurrent.futures.as_completed(future_to_file):
                    if self.cancel_operation:
                        self._ui_status("Operation cancelled.")
                        self._ui_button(self.cancel_button, tk.DISABLED)
                        return

                    src, dst = future_to_file[future]
                    try:
                        future.result()
                        copied_count += 1
                        audit_log.setdefault(os.path.dirname(dst), []).append(os.path.basename(dst))

                        if copied_count % 5 == 0 or copied_count == total:
                            self._ui_progress((copied_count / total) * 100)
                            self._ui_status(f"Copying... {copied_count}/{total} files copied.")
                    except Exception as e:
                        print(f"Error copying {src} to {dst}: {e}")

            elapsed = time() - start_time
            self._ui_status(f"Copied {copied_count} files in {elapsed:.2f} seconds.")
            self._ui_show_info("Done", f"Copied {copied_count} files.")
            self._ui_button(self.cancel_button, tk.DISABLED)
            self.write_audit_log(audit_log)
            self.scan_files()

        threading.Thread(target=do_copy, daemon=True).start()

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def write_audit_log(self, audit_log):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = os.path.normpath(
            os.path.join(self.dest_root_folder.get(), f"audit_log_{timestamp}.txt")
        )
        try:
            with open(log_file, 'w') as f:
                for folder, files in audit_log.items():
                    f.write(f"Folder: {os.path.normpath(folder)}\n")
                    for file in files:
                        f.write(f"  {file}\n")
                    f.write("\n")
            print(f"Audit log written to {log_file}")
        except Exception as e:
            print(f"Error writing audit log: {e}")

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def cancel_current_operation(self):
        self.cancel_operation = True
        self.status_text.set("Cancelling operation...")


if __name__ == "__main__":
    root = tk.Tk()
    app = PhotoOrganizer(root)
    root.mainloop()
