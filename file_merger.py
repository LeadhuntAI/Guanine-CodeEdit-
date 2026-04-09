"""
File Recovery Merger Tool
=========================
Multi-source file recovery and merge tool with Flask web UI.
Includes built-in Windsurf / VS Code / Cursor history extraction.
Compares files across multiple source directories, identifies conflicts,
and lets you review diffs before merging into a target directory.

Architecture:
    - FileScanner: Parallel directory walker with hash-based deduplication
    - MergeEngine: Diff generator and file copier with skip-if-identical logic
    - SQLite persistence: Per-session databases with WAL journaling
    - Flask UI: Setup wizard, inventory browser, conflict resolver, merge editor

Usage:
    pip install flask
    python file_merger.py

Then open http://localhost:5000 in your browser.
"""

import os
import sys
import hashlib
import json
import difflib
import logging
import threading
import fnmatch
import shutil
import time
import queue
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

from flask import (
    Flask, render_template, request, redirect, url_for,
    jsonify, Response, stream_with_context, flash
)
try:
    from markupsafe import Markup
except ImportError:
    from flask import Markup

# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class FileVersion:
    source_name: str
    source_root: str
    absolute_path: str
    relative_path: str
    file_size: int
    modified_time: float
    created_time: float
    sha256: str
    line_count: Optional[int]  # None for binary
    is_binary: bool

    def modified_dt(self):
        """Return modification time as a datetime object."""
        return datetime.fromtimestamp(self.modified_time)

    def created_dt(self):
        """Return creation time as a datetime object."""
        return datetime.fromtimestamp(self.created_time)

    def age_human(self):
        """Return human-readable age like '2 hours ago' or '3 days ago'."""
        delta = datetime.now() - self.modified_dt()
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"

    def size_human(self):
        """Return file size in human-readable format (B, KB, MB)."""
        s = self.file_size
        if s > 1_048_576:
            return f"{s / 1_048_576:.1f} MB"
        if s > 1024:
            return f"{s / 1024:.1f} KB"
        return f"{s} B"

    def to_dict(self):
        d = asdict(self)
        d['modified_str'] = self.modified_dt().strftime('%Y-%m-%d %H:%M:%S')
        d['created_str'] = self.created_dt().strftime('%Y-%m-%d %H:%M:%S')
        d['size_human'] = self.size_human()
        d['sha256_short'] = self.sha256[:12]
        return d


@dataclass
class MergeItem:
    relative_path: str
    versions: list = field(default_factory=list)
    category: str = ""       # "auto_unique", "auto_identical", "conflict"
    selected_index: int = 0  # index into versions list
    resolved: bool = False

    @property
    def selected_version(self):
        if self.versions and 0 <= self.selected_index < len(self.versions):
            return self.versions[self.selected_index]
        return None

    def source_names(self):
        return [v.source_name for v in self.versions]


@dataclass
class SourceConfig:
    name: str
    path: str
    priority: int  # lower = higher priority


# ---------------------------------------------------------------------------
# File Scanner
# ---------------------------------------------------------------------------

DEFAULT_IGNORE = {
    '.git', '__pycache__', 'node_modules', '.venv', 'venv',
    '.env', '.tox', '.mypy_cache', '.pytest_cache', 'dist',
    'build', '.eggs', '*.egg-info', 'Thumbs.db', '.DS_Store',
    '.ruff_cache', '.coverage', 'htmlcov',
}

BINARY_EXTENSIONS = {
    # Images
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.svg', '.webp',
    # Fonts
    '.woff', '.woff2', '.ttf', '.eot', '.otf',
    # Archives
    '.zip', '.rar', '.7z', '.gz', '.tar', '.bz2', '.xz', '.zst',
    # Executables / libraries
    '.exe', '.dll', '.so', '.dylib', '.pyd',
    # Documents
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.pptx',
    # Media
    '.mp3', '.mp4', '.avi', '.mov', '.wav', '.flac', '.ogg', '.webm',
    # Data
    '.sqlite3', '.db', '.pkl', '.pickle', '.parquet', '.arrow',
    # Compiled
    '.pyc', '.pyo', '.class', '.o', '.obj', '.wasm',
}

# Size limits for various operations
MAX_FILE_CONTENT_SIZE = 500_000      # 500 KB for file content preview
MAX_LINE_COUNT_SIZE = 10_000_000     # 10 MB for line counting
MAX_DIFF_LINES = 10_000              # Max lines for unified diff
MAX_SBS_DIFF_LINES = 8_000           # Max lines for side-by-side diff
MAX_SEARCH_RESULTS = 200             # Max search results


class FileScanner:
    def __init__(self, ignore_patterns=None):
        self.ignore_patterns = ignore_patterns or DEFAULT_IGNORE
        self._progress_queue = queue.Queue()
        self._scan_total = 0
        self._scan_done = 0

    def should_ignore(self, rel_path: str) -> bool:
        parts = Path(rel_path).parts
        for part in parts:
            if part in self.ignore_patterns:
                return True
            for pattern in self.ignore_patterns:
                if '*' in pattern and fnmatch.fnmatch(part, pattern):
                    return True
        name = Path(rel_path).name
        for pattern in self.ignore_patterns:
            if '*' in pattern and fnmatch.fnmatch(name, pattern):
                return True
        return False

    @staticmethod
    def compute_hash(filepath: str) -> str:
        """Compute SHA-256 hash using 64KB chunked reads for memory efficiency."""
        h = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    h.update(chunk)
        except (OSError, PermissionError):
            return "ERROR"
        return h.hexdigest()

    @staticmethod
    def detect_binary(filepath: str) -> bool:
        """Detect if a file is binary by extension or null-byte heuristic.

        Checks file extension first (fast path), then falls back to
        reading the first 8KB and looking for null bytes.
        """
        ext = Path(filepath).suffix.lower()
        if ext in BINARY_EXTENSIONS:
            return True
        try:
            with open(filepath, 'rb') as f:
                chunk = f.read(8192)
                return b'\x00' in chunk
        except (OSError, PermissionError):
            return True

    @staticmethod
    def detect_encoding(filepath: str) -> str:
        for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
            try:
                with open(filepath, encoding=enc) as f:
                    f.read(4096)
                return enc
            except (UnicodeDecodeError, UnicodeError):
                continue
        return 'latin-1'

    @staticmethod
    def count_lines(filepath: str, encoding: str) -> Optional[int]:
        try:
            with open(filepath, encoding=encoding, errors='replace') as f:
                return sum(1 for _ in f)
        except (OSError, PermissionError):
            return None

    @staticmethod
    def _normalized_content_match(versions: list) -> bool:
        """Check if all versions have identical content after normalizing
        line endings and stripping trailing whitespace per line.
        Returns True if content is effectively the same."""
        try:
            normalized = []
            for v in versions:
                enc = FileScanner.detect_encoding(v.absolute_path)
                with open(v.absolute_path, encoding=enc, errors='replace') as f:
                    # Normalize: strip trailing whitespace per line, use \n endings
                    lines = [line.rstrip() for line in f.readlines()]
                    # Also strip trailing blank lines
                    while lines and not lines[-1]:
                        lines.pop()
                    normalized.append('\n'.join(lines))
            return len(set(normalized)) == 1
        except (OSError, PermissionError):
            return False

    def _safe_path(self, path: str) -> str:
        """Add long path prefix on Windows if needed."""
        if sys.platform == 'win32' and len(path) > 250 and not path.startswith('\\\\?\\'):
            return '\\\\?\\' + os.path.abspath(path)
        return path

    def scan_file(self, source_name: str, source_root: str, abs_path: str, rel_path: str) -> Optional[FileVersion]:
        """Scan a single file: compute hash, detect binary, count lines.

        Returns a FileVersion dataclass or None if the file cannot be read.
        Files larger than MAX_LINE_COUNT_SIZE skip line counting.
        """
        try:
            safe = self._safe_path(abs_path)
            stat = os.stat(safe)
            is_binary = self.detect_binary(safe)
            sha = self.compute_hash(safe)
            line_count = None
            if not is_binary and stat.st_size < MAX_LINE_COUNT_SIZE:
                enc = self.detect_encoding(safe)
                line_count = self.count_lines(safe, enc)
            return FileVersion(
                source_name=source_name,
                source_root=source_root,
                absolute_path=abs_path,
                relative_path=rel_path,
                file_size=stat.st_size,
                modified_time=stat.st_mtime,
                created_time=stat.st_ctime,
                sha256=sha,
                line_count=line_count,
                is_binary=is_binary,
            )
        except (OSError, PermissionError) as e:
            self._progress_queue.put(('error', f"Error scanning {rel_path}: {e}"))
            return None

    def scan_source(self, source: SourceConfig) -> dict:
        """Scan a source directory and return dict of rel_path -> FileVersion."""
        result = {}
        root = source.path
        if not os.path.isdir(root):
            self._progress_queue.put(('error', f"Source not found: {root}"))
            return result

        # Directory scan
        # First pass: collect all file paths
        file_paths = []
        for dirpath, dirnames, filenames in os.walk(root):
            # Filter ignored dirs in-place
            dirnames[:] = [d for d in dirnames if d not in self.ignore_patterns]
            for fname in filenames:
                abs_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abs_path, root)
                if not self.should_ignore(rel_path):
                    file_paths.append((abs_path, rel_path))

        self._progress_queue.put(('source_count', source.name, len(file_paths)))

        # Second pass: scan files with thread pool
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {}
            for abs_path, rel_path in file_paths:
                fut = pool.submit(self.scan_file, source.name, root, abs_path, rel_path)
                futures[fut] = rel_path

            for fut in as_completed(futures):
                rel_path = futures[fut]
                try:
                    version = fut.result()
                    if version:
                        # Normalize path separators
                        key = rel_path.replace('\\', '/')
                        version.relative_path = key
                        result[key] = version
                except Exception as e:
                    self._progress_queue.put(('error', f"Error: {rel_path}: {e}"))
                self._scan_done += 1
                if self._scan_done % 50 == 0:
                    self._progress_queue.put(('progress', self._scan_done, self._scan_total))

        self._progress_queue.put(('source_done', source.name, len(result)))
        return result

    def build_inventory(self, sources: list) -> dict:
        """Scan all sources and build merged inventory."""
        self._scan_done = 0
        self._scan_total = 0

        # Count total files first (quick walk)
        for src in sources:
            if os.path.isdir(src.path):
                if self._is_editor_history_dir(src.path):
                    # For editor history dirs, count subdirectories (each = one file)
                    try:
                        self._scan_total += sum(
                            1 for d in os.listdir(src.path)
                            if os.path.isdir(os.path.join(src.path, d))
                        )
                    except OSError:
                        pass
                else:
                    for dirpath, dirnames, filenames in os.walk(src.path):
                        dirnames[:] = [d for d in dirnames if d not in self.ignore_patterns]
                        self._scan_total += len(filenames)

        self._progress_queue.put(('total', self._scan_total))

        inventory = {}
        for src in sources:
            self._progress_queue.put(('scanning', src.name))
            source_files = self.scan_source(src)
            for key, version in source_files.items():
                if key not in inventory:
                    inventory[key] = MergeItem(relative_path=key)
                inventory[key].versions.append(version)

        # Categorize
        for item in inventory.values():
            self._categorize(item)

        self._progress_queue.put(('done', len(inventory)))
        return inventory

    @staticmethod
    def _categorize(item: MergeItem):
        if len(item.versions) == 1:
            item.category = "auto_unique"
            item.selected_index = 0
            item.resolved = True
        else:
            hashes = set(v.sha256 for v in item.versions)
            if len(hashes) == 1:
                item.category = "auto_identical"
                item.selected_index = 0
                item.resolved = True
            else:
                # Same file size? Check if content differs only in line endings/trailing whitespace
                sizes = set(v.file_size for v in item.versions)
                content_match = False
                if not any(v.is_binary for v in item.versions):
                    content_match = FileScanner._normalized_content_match(item.versions)

                if content_match:
                    # Content is effectively identical — pick latest, auto-resolve
                    item.category = "auto_identical"
                    latest_idx = 0
                    latest_time = 0
                    for i, v in enumerate(item.versions):
                        if v.modified_time > latest_time:
                            latest_time = v.modified_time
                            latest_idx = i
                    item.selected_index = latest_idx
                    item.resolved = True
                else:
                    item.category = "conflict"
                    # Default: pick the version with most lines (fallback to largest size for binary)
                    best_idx = 0
                    best_lines = -1
                    for i, v in enumerate(item.versions):
                        lc = v.line_count if v.line_count is not None else -1
                        if lc > best_lines:
                            best_lines = lc
                            best_idx = i
                    # If all versions are binary (no line counts), fall back to largest size
                    if best_lines <= 0:
                        best_size = 0
                        for i, v in enumerate(item.versions):
                            if v.file_size > best_size:
                                best_size = v.file_size
                                best_idx = i
                    item.selected_index = best_idx
                    item.resolved = False


# ---------------------------------------------------------------------------
# Merge Engine
# ---------------------------------------------------------------------------

class MergeEngine:
    def __init__(self):
        self.log_entries = []

    def log(self, action: str, path: str = "", source: str = "", details: str = ""):
        self.log_entries.append({
            'timestamp': datetime.now().isoformat(),
            'action': action,
            'path': path,
            'source': source,
            'details': details,
        })

    def generate_diff(self, version_a: FileVersion, version_b: FileVersion) -> list:
        """Generate unified diff lines between two text file versions."""
        if version_a.is_binary or version_b.is_binary:
            return [f"Binary files differ (SHA256: {version_a.sha256[:12]} vs {version_b.sha256[:12]})"]

        try:
            enc_a = FileScanner.detect_encoding(version_a.absolute_path)
            enc_b = FileScanner.detect_encoding(version_b.absolute_path)
            with open(version_a.absolute_path, encoding=enc_a, errors='replace') as f:
                lines_a = f.readlines()
            with open(version_b.absolute_path, encoding=enc_b, errors='replace') as f:
                lines_b = f.readlines()

            # Limit for very large files
            if len(lines_a) > 10000 or len(lines_b) > 10000:
                lines_a = lines_a[:10000]
                lines_b = lines_b[:10000]

            diff = list(difflib.unified_diff(
                lines_a, lines_b,
                fromfile=f"{version_a.source_name}: {version_a.relative_path}",
                tofile=f"{version_b.source_name}: {version_b.relative_path}",
                lineterm=''
            ))
            return diff if diff else ["Files are identical in content (only metadata differs)"]
        except Exception as e:
            return [f"Error generating diff: {e}"]

    def execute_merge(self, inventory: dict, target_dir: str, progress_queue: queue.Queue):
        """Copy all files that have a selected version to target directory.
        Skips files that already exist at the target with identical content."""
        resolved = [item for item in inventory.values() if item.selected_version]
        total = len(resolved)
        new_files = 0
        updated = 0
        skipped = 0
        errors = 0
        new_file_list = []
        updated_file_list = []

        progress_queue.put(('merge_start', total))

        for item in resolved:
            version = item.selected_version
            dest_path = os.path.join(target_dir, item.relative_path.replace('/', os.sep))
            try:
                dest_dir = os.path.dirname(dest_path)
                os.makedirs(dest_dir, exist_ok=True)

                is_new = not os.path.isfile(dest_path)

                # Skip if target exists and has identical content
                if not is_new:
                    existing_hash = FileScanner.compute_hash(dest_path)
                    if existing_hash == version.sha256:
                        skipped += 1
                        processed = new_files + updated + skipped + errors
                        if processed % 50 == 0:
                            progress_queue.put(('merge_progress', processed, total))
                        continue

                shutil.copy2(version.absolute_path, dest_path)

                if is_new:
                    new_files += 1
                    new_file_list.append(item.relative_path)
                    self.log('new_file', item.relative_path, version.source_name,
                             f"Size: {version.size_human()}")
                else:
                    updated += 1
                    updated_file_list.append(item.relative_path)
                    self.log('updated', item.relative_path, version.source_name,
                             f"Size: {version.size_human()}")
            except (OSError, PermissionError) as e:
                errors += 1
                self.log('error', item.relative_path, version.source_name, str(e))

            processed = new_files + updated + skipped + errors
            if processed % 20 == 0:
                progress_queue.put(('merge_progress', processed, total))

        progress_queue.put(('merge_done', new_files, updated, skipped, errors))
        self.log('merge_complete', '', '',
                 f"New: {new_files}, Updated: {updated}, Skipped: {skipped}, Errors: {errors}")
        return {
            'new_files': new_files, 'updated': updated, 'skipped': skipped,
            'errors': errors, 'total': total,
            'new_file_list': sorted(new_file_list),
            'updated_file_list': sorted(updated_file_list),
        }

    def save_log(self, filepath: str):
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.log_entries, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Side-by-Side Diff Generator
# ---------------------------------------------------------------------------

def generate_side_by_side_diff(version_a: 'FileVersion', version_b: 'FileVersion',
                                context_lines: int = 3) -> list:
    """
    Generate a side-by-side diff suitable for rendering in a git-merge-style view.
    Returns list of dicts: {type, left_num, left_line, right_num, right_line}
    type is one of: 'equal', 'delete', 'insert', 'replace'
    """
    if version_a.is_binary or version_b.is_binary:
        return [{'type': 'binary', 'left_num': '', 'left_line': 'Binary file',
                 'right_num': '', 'right_line': 'Binary file'}]

    try:
        enc_a = FileScanner.detect_encoding(version_a.absolute_path)
        enc_b = FileScanner.detect_encoding(version_b.absolute_path)
        with open(version_a.absolute_path, encoding=enc_a, errors='replace') as f:
            lines_a = f.readlines()
        with open(version_b.absolute_path, encoding=enc_b, errors='replace') as f:
            lines_b = f.readlines()
    except Exception as e:
        return [{'type': 'error', 'left_num': '', 'left_line': str(e),
                 'right_num': '', 'right_line': ''}]

    # Truncate very large files
    max_lines = 8000
    lines_a = lines_a[:max_lines]
    lines_b = lines_b[:max_lines]

    # Strip trailing newlines for display
    lines_a = [l.rstrip('\n\r') for l in lines_a]
    lines_b = [l.rstrip('\n\r') for l in lines_b]

    matcher = difflib.SequenceMatcher(None, lines_a, lines_b)
    result = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for i, j in zip(range(i1, i2), range(j1, j2)):
                result.append({
                    'type': 'equal',
                    'left_num': i + 1, 'left_line': lines_a[i],
                    'right_num': j + 1, 'right_line': lines_b[j],
                })
        elif tag == 'delete':
            for i in range(i1, i2):
                result.append({
                    'type': 'delete',
                    'left_num': i + 1, 'left_line': lines_a[i],
                    'right_num': '', 'right_line': '',
                })
        elif tag == 'insert':
            for j in range(j1, j2):
                result.append({
                    'type': 'insert',
                    'left_num': '', 'left_line': '',
                    'right_num': j + 1, 'right_line': lines_b[j],
                })
        elif tag == 'replace':
            max_len = max(i2 - i1, j2 - j1)
            for k in range(max_len):
                left_i = i1 + k if k < (i2 - i1) else None
                right_j = j1 + k if k < (j2 - j1) else None
                result.append({
                    'type': 'replace',
                    'left_num': (left_i + 1) if left_i is not None else '',
                    'left_line': lines_a[left_i] if left_i is not None else '',
                    'right_num': (right_j + 1) if right_j is not None else '',
                    'right_line': lines_b[right_j] if right_j is not None else '',
                })

    return result


# ---------------------------------------------------------------------------
# Session Persistence — SQLite
# ---------------------------------------------------------------------------
# Each session is stored in its own folder under SESSIONS_DIR:
#   sessions/
#     _active.json          — which session is active
#     <session-id>/
#       session.db           — SQLite database with all data
#       config.json          — (legacy, read for migration)
#       inventory.json       — (legacy, read for migration)
#       meta.json            — (legacy, read for migration)
#
# The SQLite database has these tables:
#   meta         — key/value pairs (session name, timestamps, etc.)
#   config       — key/value pairs (sources JSON, target_dir, etc.)
#   merge_items  — one row per relative_path (category, selected_index, resolved)
#   file_versions — one row per (relative_path, version_index)
#   coverage     — per-source stats snapshot
#
# Resolving a single conflict = one UPDATE on merge_items (instant).
# Full inventory save = bulk INSERT (only after scan).
# ---------------------------------------------------------------------------

SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')

# Thread-local storage for SQLite connections (SQLite objects can't cross threads)
_db_local = threading.local()


def _session_dir(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, session_id)


def _ensure_sessions_dir():
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def _generate_session_id() -> str:
    """Generate a short unique session id based on timestamp."""
    return datetime.now().strftime('%Y%m%d_%H%M%S')


def _db_path(session_id: str) -> str:
    return os.path.join(_session_dir(session_id), 'session.db')


def _get_db(session_id: str) -> sqlite3.Connection:
    """Get or create a SQLite connection for the given session.
    Uses WAL mode for concurrent reads during background operations."""
    key = f'db_{session_id}'
    conn = getattr(_db_local, key, None)
    if conn is not None:
        try:
            conn.execute('SELECT 1')
            return conn
        except sqlite3.Error:
            conn = None
    sdir = _session_dir(session_id)
    os.makedirs(sdir, exist_ok=True)
    db_file = _db_path(session_id)
    conn = sqlite3.connect(db_file, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA cache_size=-8000')  # 8MB cache
    conn.row_factory = sqlite3.Row
    setattr(_db_local, key, conn)
    _init_db_schema(conn)
    return conn


def _close_db(session_id: str):
    """Close the SQLite connection for a session."""
    key = f'db_{session_id}'
    conn = getattr(_db_local, key, None)
    if conn:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        setattr(_db_local, key, None)


def _init_db_schema(conn: sqlite3.Connection):
    """Create tables if they don't exist."""
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS merge_items (
            relative_path  TEXT PRIMARY KEY,
            category       TEXT NOT NULL,
            selected_index INTEGER NOT NULL DEFAULT 0,
            resolved       INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS file_versions (
            relative_path  TEXT NOT NULL,
            version_index  INTEGER NOT NULL,
            source_name    TEXT NOT NULL,
            source_root    TEXT NOT NULL,
            absolute_path  TEXT NOT NULL,
            file_size      INTEGER NOT NULL,
            modified_time  REAL NOT NULL,
            created_time   REAL NOT NULL,
            sha256         TEXT NOT NULL,
            line_count     INTEGER,
            is_binary      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (relative_path, version_index)
        );
        CREATE TABLE IF NOT EXISTS coverage (
            source_name    TEXT PRIMARY KEY,
            total_files    INTEGER,
            unique_files   INTEGER,
            conflict_files INTEGER,
            identical_files INTEGER,
            snapshot_at    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fv_path ON file_versions(relative_path);
        CREATE INDEX IF NOT EXISTS idx_mi_category ON merge_items(category);
        CREATE INDEX IF NOT EXISTS idx_mi_resolved ON merge_items(resolved);
    ''')


# ---------------------------------------------------------------------------
# Session list / active / meta — still uses _active.json + meta in SQLite
# ---------------------------------------------------------------------------

def list_sessions() -> list:
    """Return list of all sessions with metadata, newest first."""
    _ensure_sessions_dir()
    sessions = []
    try:
        for name in os.listdir(SESSIONS_DIR):
            sdir = os.path.join(SESSIONS_DIR, name)
            if not os.path.isdir(sdir):
                continue
            db_file = os.path.join(sdir, 'session.db')
            meta_json = os.path.join(sdir, 'meta.json')
            if os.path.isfile(db_file):
                # Read from SQLite
                try:
                    conn = sqlite3.connect(db_file, timeout=5)
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute('SELECT key, value FROM meta').fetchall()
                    meta = {r['key']: r['value'] for r in rows}
                    meta['id'] = name
                    # Check inventory count
                    cnt = conn.execute('SELECT COUNT(*) FROM merge_items').fetchone()[0]
                    meta['has_inventory'] = cnt > 0
                    meta['inventory_size'] = cnt
                    # Parse stats from meta if present
                    if meta.get('stats'):
                        try:
                            meta['stats'] = json.loads(meta['stats'])
                        except (json.JSONDecodeError, TypeError):
                            pass
                    # Parse sources list
                    if meta.get('sources'):
                        try:
                            meta['sources'] = json.loads(meta['sources'])
                        except (json.JSONDecodeError, TypeError):
                            pass
                    conn.close()
                    sessions.append(meta)
                except sqlite3.Error:
                    pass
    except OSError:
        pass
    sessions.sort(key=lambda s: s.get('updated_at', ''), reverse=True)
    return sessions


def get_active_session_id() -> str:
    """Get the currently active session ID, or empty string."""
    _ensure_sessions_dir()
    active_file = os.path.join(SESSIONS_DIR, '_active.json')
    try:
        if os.path.isfile(active_file):
            with open(active_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                sid = data.get('session_id', '')
                if sid and os.path.isdir(_session_dir(sid)):
                    return sid
    except (OSError, json.JSONDecodeError):
        pass
    return ''


def set_active_session(session_id: str):
    """Set the active session ID."""
    _ensure_sessions_dir()
    active_file = os.path.join(SESSIONS_DIR, '_active.json')
    try:
        with open(active_file, 'w', encoding='utf-8') as f:
            json.dump({'session_id': session_id}, f)
    except OSError as e:
        print(f"[WARNING] Failed to write active session: {e}")


def save_session_meta(session_id: str, session_name: str = ''):
    """Create/update session metadata in SQLite."""
    conn = _get_db(session_id)
    now = datetime.now().isoformat()

    # Read existing created_at
    row = conn.execute("SELECT value FROM meta WHERE key='created_at'").fetchone()
    if not row:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('created_at', ?)", (now,))

    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('updated_at', ?)", (now,))

    if session_name:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('name', ?)", (session_name,))
    else:
        row = conn.execute("SELECT value FROM meta WHERE key='name'").fetchone()
        if not row:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('name', ?)",
                         (f'Session {session_id}',))

    if state.get('sources'):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('sources', ?)",
                     (json.dumps([s.name for s in state['sources']]),))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('target_dir', ?)",
                     (state.get('target_dir', ''),))

    if state.get('inventory'):
        stats = _compute_stats(state['inventory'])
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('stats', ?)",
                     (json.dumps(stats),))

    conn.commit()


def save_config(sources, target_dir, ignore_patterns):
    """Save source config to SQLite."""
    sid = state.get('_session_id', '')
    if not sid:
        return
    conn = _get_db(sid)
    data = {
        'sources': [{'name': s.name, 'path': s.path, 'priority': s.priority} for s in sources],
        'target_dir': target_dir,
        'ignore_patterns': sorted(ignore_patterns),
        'saved_at': datetime.now().isoformat(),
    }
    for k, v in data.items():
        val = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, val))
    conn.commit()


def load_config(session_id: str = '') -> dict:
    """Load config from SQLite."""
    sid = session_id or state.get('_session_id', '')
    if not sid:
        return None

    # Try SQLite
    db_file = _db_path(sid)
    if os.path.isfile(db_file):
        try:
            conn = _get_db(sid)
            rows = conn.execute('SELECT key, value FROM config').fetchall()
            if rows:
                data = {}
                for r in rows:
                    try:
                        data[r['key']] = json.loads(r['value'])
                    except (json.JSONDecodeError, TypeError):
                        data[r['key']] = r['value']
                if data.get('sources'):
                    return data
        except sqlite3.Error as e:
            print(f"[WARNING] Failed to load config from SQLite: {e}")

    # Fallback: legacy JSON
    config_path = os.path.join(_session_dir(sid), 'config.json')
    try:
        if os.path.isfile(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    return None


def save_inventory_state(inventory):
    """Bulk-save entire inventory to SQLite. Used after scan completion."""
    sid = state.get('_session_id', '')
    if not sid:
        return
    conn = _get_db(sid)

    # Use a transaction for bulk insert performance
    conn.execute('DELETE FROM merge_items')
    conn.execute('DELETE FROM file_versions')

    items_batch = []
    versions_batch = []
    for key, item in inventory.items():
        items_batch.append((
            item.relative_path,
            item.category,
            item.selected_index,
            1 if item.resolved else 0,
        ))
        for vi, v in enumerate(item.versions):
            versions_batch.append((
                item.relative_path, vi,
                v.source_name, v.source_root, v.absolute_path,
                v.file_size, v.modified_time, v.created_time,
                v.sha256, v.line_count, 1 if v.is_binary else 0,
            ))

    conn.executemany(
        'INSERT INTO merge_items (relative_path, category, selected_index, resolved) VALUES (?,?,?,?)',
        items_batch
    )
    conn.executemany(
        'INSERT INTO file_versions (relative_path, version_index, source_name, source_root, '
        'absolute_path, file_size, modified_time, created_time, sha256, line_count, is_binary) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        versions_batch
    )
    conn.commit()
    print(f"[INFO] Saved {len(items_batch)} items + {len(versions_batch)} versions to SQLite")


def save_item_resolution(relative_path: str, selected_index: int, resolved: bool):
    """Update a single merge item's resolution state. Instant — no full inventory write."""
    sid = state.get('_session_id', '')
    if not sid:
        return
    conn = _get_db(sid)
    conn.execute(
        'UPDATE merge_items SET selected_index=?, resolved=? WHERE relative_path=?',
        (selected_index, 1 if resolved else 0, relative_path)
    )
    conn.commit()


def save_batch_resolutions(updates: list):
    """Batch-update multiple merge items' resolution state.
    updates: list of (relative_path, selected_index, resolved)"""
    sid = state.get('_session_id', '')
    if not sid:
        return
    conn = _get_db(sid)
    conn.executemany(
        'UPDATE merge_items SET selected_index=?, resolved=? WHERE relative_path=?',
        [(idx, 1 if res else 0, path) for path, idx, res in updates]
    )
    conn.commit()


def load_inventory_state(session_id: str = ''):
    """Load saved inventory state from SQLite."""
    sid = session_id or state.get('_session_id', '')
    if not sid:
        return None

    # Try SQLite
    db_file = _db_path(sid)
    if os.path.isfile(db_file):
        try:
            conn = _get_db(sid)
            item_count = conn.execute('SELECT COUNT(*) FROM merge_items').fetchone()[0]
            if item_count > 0:
                return _load_inventory_from_db(conn)
        except sqlite3.Error as e:
            print(f"[WARNING] Failed to load inventory from SQLite: {e}")

    return None


def _load_inventory_from_db(conn: sqlite3.Connection) -> dict:
    """Load full inventory from SQLite into MergeItem dict."""
    inventory = {}

    # Load all merge items
    items = conn.execute('SELECT * FROM merge_items').fetchall()
    # Load all versions, grouped by relative_path
    versions_rows = conn.execute(
        'SELECT * FROM file_versions ORDER BY relative_path, version_index'
    ).fetchall()

    # Group versions by path
    versions_by_path = {}
    for vr in versions_rows:
        rp = vr['relative_path']
        if rp not in versions_by_path:
            versions_by_path[rp] = []
        versions_by_path[rp].append(FileVersion(
            source_name=vr['source_name'],
            source_root=vr['source_root'],
            absolute_path=vr['absolute_path'],
            relative_path=rp,
            file_size=vr['file_size'],
            modified_time=vr['modified_time'],
            created_time=vr['created_time'],
            sha256=vr['sha256'],
            line_count=vr['line_count'],
            is_binary=bool(vr['is_binary']),
        ))

    for row in items:
        rp = row['relative_path']
        item = MergeItem(
            relative_path=rp,
            versions=versions_by_path.get(rp, []),
            category=row['category'],
            selected_index=row['selected_index'],
            resolved=bool(row['resolved']),
        )
        inventory[rp] = item

    return inventory if inventory else None


def _auto_save_state():
    """Save config and session meta. Does NOT re-save full inventory
    (individual resolve operations use save_item_resolution instead)."""
    sid = state.get('_session_id', '')
    if not sid:
        sid = _generate_session_id()
        state['_session_id'] = sid
        set_active_session(sid)

    if state.get('sources'):
        save_config(state['sources'], state['target_dir'], state['ignore_patterns'])
    save_session_meta(sid)


def _auto_save_full():
    """Save everything including full inventory. Used after scan or bulk operations."""
    sid = state.get('_session_id', '')
    if not sid:
        sid = _generate_session_id()
        state['_session_id'] = sid
        set_active_session(sid)

    if state.get('sources'):
        save_config(state['sources'], state['target_dir'], state['ignore_patterns'])
    if state.get('inventory'):
        save_inventory_state(state['inventory'])
    save_session_meta(sid)


def delete_session(session_id: str):
    """Delete a session and all its data."""
    _close_db(session_id)
    sdir = _session_dir(session_id)
    if os.path.isdir(sdir):
        shutil.rmtree(sdir, ignore_errors=True)
    if state.get('_session_id') == session_id:
        state['_session_id'] = ''
        set_active_session('')


# ---------------------------------------------------------------------------
# Flask Application
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = 'file-merger-recovery-tool-2026'

# Register agent review Blueprint
from agent_review import agent_bp
app.register_blueprint(agent_bp, url_prefix='/agent')

# Global state
state = {
    'sources': [],
    'target_dir': '',
    'ignore_patterns': DEFAULT_IGNORE.copy(),
    'inventory': {},
    'scanner': None,
    'engine': MergeEngine(),
    'scan_status': 'idle',  # idle, scanning, done
    'scan_messages': [],
    'merge_status': 'idle',
    'merge_result': None,
}

# --- Auto-load saved session on startup ---
def _restore_session():
    """Restore the active session on startup."""
    sid = get_active_session_id()
    if not sid:
        return

    state['_session_id'] = sid
    print(f"[INFO] Restoring session: {sid}")

    cfg = load_config(sid)
    if cfg:
        state['sources'] = [
            SourceConfig(name=s['name'], path=s['path'], priority=s.get('priority', i))
            for i, s in enumerate(cfg.get('sources', []))
        ]
        state['target_dir'] = cfg.get('target_dir', '')
        ip = cfg.get('ignore_patterns')
        if ip:
            state['ignore_patterns'] = set(ip)
        print(f"[INFO]   Config loaded: {len(state['sources'])} sources, target={state['target_dir']}")

    inv = load_inventory_state(sid)
    if inv:
        state['inventory'] = inv
        state['scan_status'] = 'done'
        total = len(inv)
        conflicts = sum(1 for i in inv.values() if i.category == 'conflict')
        resolved = sum(1 for i in inv.values() if i.category == 'conflict' and i.resolved)
        print(f"[INFO]   Inventory loaded: {total} files, "
              f"{conflicts} conflicts ({resolved} resolved)")
    else:
        print("[INFO]   No inventory found for this session.")

_restore_session()


@app.route('/')
def index():
    return redirect(url_for('ide_view'))


@app.route('/session/switch/<session_id>')
def switch_session(session_id):
    """Switch to a different session."""
    sdir = _session_dir(session_id)
    if not os.path.isdir(sdir):
        flash(f'Session {session_id} not found.', 'error')
        return redirect(url_for('setup'))

    # Save current session first
    old_sid = state.get('_session_id', '')
    if old_sid and state.get('inventory'):
        _auto_save_state()
        _close_db(old_sid)

    # Load the new session
    state['_session_id'] = session_id
    set_active_session(session_id)
    state['inventory'] = {}
    state['scan_status'] = 'idle'
    state['sources'] = []
    state['target_dir'] = ''
    state['engine'] = MergeEngine()

    cfg = load_config(session_id)
    if cfg:
        state['sources'] = [
            SourceConfig(name=s['name'], path=s['path'], priority=s.get('priority', i))
            for i, s in enumerate(cfg.get('sources', []))
        ]
        state['target_dir'] = cfg.get('target_dir', '')
        ip = cfg.get('ignore_patterns')
        if ip:
            state['ignore_patterns'] = set(ip)
    inv = load_inventory_state(session_id)
    if inv:
        state['inventory'] = inv
        state['scan_status'] = 'done'
        stats = _compute_stats(inv)
        flash(f'Switched to session {session_id}: {stats["total"]} files, '
              f'{stats["conflicts"]} conflicts ({stats["resolved"]} resolved).', 'success')
    else:
        flash(f'Switched to session {session_id}. No scan data — run a scan.', 'info')

    return redirect(url_for('setup'))


@app.route('/session/new')
def new_session():
    """Create a new empty session."""
    if state.get('_session_id') and state.get('inventory'):
        _auto_save_state()

    sid = _generate_session_id()
    state['_session_id'] = sid
    set_active_session(sid)
    state['inventory'] = {}
    state['scan_status'] = 'idle'
    state['sources'] = []
    state['target_dir'] = ''
    state['ignore_patterns'] = DEFAULT_IGNORE.copy()
    state['engine'] = MergeEngine()
    save_session_meta(sid, '')
    flash(f'New session created: {sid}', 'success')
    return redirect(url_for('setup'))


@app.route('/session/delete/<session_id>')
def delete_session_route(session_id):
    """Delete a session."""
    if session_id == state.get('_session_id'):
        flash('Cannot delete the active session. Switch to another first.', 'error')
        return redirect(url_for('setup'))
    delete_session(session_id)
    flash(f'Session {session_id} deleted.', 'success')
    return redirect(url_for('setup'))


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if request.method == 'POST':
        sources = []
        i = 0
        while f'source_name_{i}' in request.form:
            name = request.form.get(f'source_name_{i}', '').strip()
            path = request.form.get(f'source_path_{i}', '').strip()
            if name and path:
                sources.append(SourceConfig(name=name, path=path, priority=i))
            i += 1

        target = request.form.get('target_dir', '').strip()
        ignore_raw = request.form.get('ignore_patterns', '').strip()
        ignore_set = set(p.strip() for p in ignore_raw.split(',') if p.strip())

        if not sources:
            flash('Please add at least one source directory.', 'error')
            return redirect(url_for('setup'))
        if not target:
            flash('Please specify a target directory.', 'error')
            return redirect(url_for('setup'))

        # Validate paths
        for src in sources:
            if not os.path.isdir(src.path):
                flash(f'Source directory not found: {src.path}', 'error')
                return redirect(url_for('setup'))

        state['sources'] = sources
        state['target_dir'] = target
        state['ignore_patterns'] = ignore_set if ignore_set else DEFAULT_IGNORE.copy()

        # Create or reuse session
        if not state.get('_session_id'):
            sid = _generate_session_id()
            state['_session_id'] = sid
            set_active_session(sid)
        save_config(sources, target, state['ignore_patterns'])
        save_session_meta(state['_session_id'],
                          request.form.get('session_name', '').strip())

        # Start scan in background
        state['scan_status'] = 'scanning'
        state['scan_messages'] = []
        state['inventory'] = {}

        scanner = FileScanner(state['ignore_patterns'])
        state['scanner'] = scanner

        def run_scan():
            try:
                inv = scanner.build_inventory(state['sources'])
                state['inventory'] = inv
                state['scan_status'] = 'done'
                state['engine'].log('scan_complete', '', '',
                                    f"Total files: {len(inv)}")
                _auto_save_full()
            except Exception as e:
                state['scan_status'] = 'error'
                state['scan_messages'].append(f"Scan error: {e}")

        thread = threading.Thread(target=run_scan, daemon=True)
        thread.start()

        return redirect(url_for('scan_progress'))

    # Default sources for GET
    default_sources = state.get('sources', [])
    if not default_sources:
        default_sources = [
            SourceConfig("Base Project", "", 0),
            SourceConfig("GitHub Repo", "", 1),
            SourceConfig("Old Backup", "", 2),
        ]

    ignore_str = ', '.join(sorted(state['ignore_patterns']))

    # Check if we have a saved session to resume
    has_saved_state = bool(state.get('inventory'))
    saved_stats = _compute_stats(state['inventory']) if has_saved_state else None

    # Session info
    current_session = state.get('_session_id', '')
    all_sessions = list_sessions()
    config_saved_at = None
    for s in all_sessions:
        if s['id'] == current_session:
            config_saved_at = s.get('updated_at', '')[:19].replace('T', ' ')
            break

    return render_template('setup.html',
                           sources=default_sources,
                           target_dir=state.get('target_dir', ''),
                           ignore_patterns=ignore_str,
                           has_saved_state=has_saved_state,
                           saved_stats=saved_stats,
                           config_saved_at=config_saved_at,
                           current_session=current_session,
                           all_sessions=all_sessions)


@app.route('/scan-progress')
def scan_progress():
    return render_template('scan_progress.html')


@app.route('/scan-events')
def scan_events():
    """SSE endpoint for scan progress."""
    def generate():
        scanner = state.get('scanner')
        if not scanner:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No scanner'})}\n\n"
            return

        q = scanner._progress_queue
        while True:
            try:
                msg = q.get(timeout=1)
                if msg[0] == 'done':
                    inv = state['inventory']
                    stats = _compute_stats(inv)
                    yield f"data: {json.dumps({'type': 'done', 'stats': stats})}\n\n"
                    return
                elif msg[0] == 'total':
                    yield f"data: {json.dumps({'type': 'total', 'count': msg[1]})}\n\n"
                elif msg[0] == 'scanning':
                    yield f"data: {json.dumps({'type': 'scanning', 'source': msg[1]})}\n\n"
                elif msg[0] == 'source_count':
                    yield f"data: {json.dumps({'type': 'source_count', 'source': msg[1], 'count': msg[2]})}\n\n"
                elif msg[0] == 'source_done':
                    yield f"data: {json.dumps({'type': 'source_done', 'source': msg[1], 'count': msg[2]})}\n\n"
                elif msg[0] == 'progress':
                    yield f"data: {json.dumps({'type': 'progress', 'done': msg[1], 'total': msg[2]})}\n\n"
                elif msg[0] == 'error':
                    yield f"data: {json.dumps({'type': 'error', 'message': msg[1]})}\n\n"
            except queue.Empty:
                if state['scan_status'] == 'done':
                    inv = state['inventory']
                    stats = _compute_stats(inv)
                    yield f"data: {json.dumps({'type': 'done', 'stats': stats})}\n\n"
                    return
                elif state['scan_status'] == 'error':
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Scan failed'})}\n\n"
                    return
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


def _compute_stats(inventory: dict) -> dict:
    total = len(inventory)
    auto_unique = sum(1 for i in inventory.values() if i.category == 'auto_unique')
    auto_identical = sum(1 for i in inventory.values() if i.category == 'auto_identical')
    conflicts = sum(1 for i in inventory.values() if i.category == 'conflict')
    resolved = sum(1 for i in inventory.values() if i.category == 'conflict' and i.resolved)
    return {
        'total': total,
        'auto_unique': auto_unique,
        'auto_identical': auto_identical,
        'conflicts': conflicts,
        'resolved': resolved,
        'auto_merge': auto_unique + auto_identical,
    }


@app.route('/inventory')
def inventory():
    inv = state['inventory']
    if not inv:
        flash('No scan results. Please run a scan first.', 'warning')
        return redirect(url_for('setup'))

    stats = _compute_stats(inv)

    # Filtering
    filter_type = request.args.get('filter', 'all')
    search = request.args.get('search', '').lower()
    page = int(request.args.get('page', 1))
    per_page = 100
    sort_by = request.args.get('sort', 'path')
    sort_dir = request.args.get('dir', 'asc')

    items = list(inv.values())

    if filter_type == 'auto':
        items = [i for i in items if i.category in ('auto_unique', 'auto_identical')]
    elif filter_type == 'conflicts':
        items = [i for i in items if i.category == 'conflict']
    elif filter_type == 'unresolved':
        items = [i for i in items if i.category == 'conflict' and not i.resolved]

    if search:
        items = [i for i in items if search in i.relative_path.lower()]

    # Sort
    if sort_by == 'path':
        items.sort(key=lambda i: i.relative_path, reverse=(sort_dir == 'desc'))
    elif sort_by == 'size':
        items.sort(key=lambda i: max(v.file_size for v in i.versions), reverse=(sort_dir == 'desc'))
    elif sort_by == 'modified':
        items.sort(key=lambda i: max(v.modified_time for v in i.versions), reverse=(sort_dir == 'desc'))
    elif sort_by == 'sources':
        items.sort(key=lambda i: len(i.versions), reverse=(sort_dir == 'desc'))

    # Paginate
    total_items = len(items)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]

    # Prepare display data
    display_items = []
    for item in page_items:
        selected = item.selected_version
        display_items.append({
            'relative_path': item.relative_path,
            'category': item.category,
            'resolved': item.resolved,
            'source_names': ', '.join(item.source_names()),
            'num_versions': len(item.versions),
            'size': selected.size_human() if selected else '',
            'modified': selected.modified_dt().strftime('%Y-%m-%d %H:%M') if selected else '',
            'lines': selected.line_count if selected else '',
            'selected_source': selected.source_name if selected else '',
        })

    return render_template('inventory.html',
                           items=display_items,
                           stats=stats,
                           filter_type=filter_type,
                           search=search,
                           page=page,
                           total_pages=total_pages,
                           total_items=total_items,
                           sort_by=sort_by,
                           sort_dir=sort_dir)


@app.route('/conflicts')
def conflicts():
    inv = state['inventory']
    conflict_items = [i for i in inv.values() if i.category == 'conflict']
    conflict_items.sort(key=lambda i: i.relative_path)

    stats = _compute_stats(inv)
    resolved_count = sum(1 for i in conflict_items if i.resolved)

    display = []
    for idx, item in enumerate(conflict_items):
        selected = item.selected_version
        display.append({
            'index': idx,
            'relative_path': item.relative_path,
            'resolved': item.resolved,
            'num_versions': len(item.versions),
            'source_names': ', '.join(item.source_names()),
            'selected_source': selected.source_name if selected else '',
            'size': selected.size_human() if selected else '',
            'modified': selected.modified_dt().strftime('%Y-%m-%d %H:%M') if selected else '',
        })

    return render_template('conflicts.html',
                           items=display,
                           total=len(conflict_items),
                           resolved=resolved_count,
                           stats=stats)


@app.route('/conflict/<path:filepath>')
def conflict_detail(filepath):
    inv = state['inventory']
    item = inv.get(filepath)
    if not item or item.category != 'conflict':
        flash('File not found or not a conflict.', 'error')
        return redirect(url_for('conflicts'))

    # Get conflict list for prev/next navigation
    conflict_items = sorted(
        [i for i in inv.values() if i.category == 'conflict'],
        key=lambda i: i.relative_path
    )
    conflict_paths = [i.relative_path for i in conflict_items]
    current_idx = conflict_paths.index(filepath) if filepath in conflict_paths else 0
    prev_path = conflict_paths[current_idx - 1] if current_idx > 0 else None
    next_path = conflict_paths[current_idx + 1] if current_idx < len(conflict_paths) - 1 else None

    # Prepare versions data
    versions = []
    for i, v in enumerate(item.versions):
        versions.append({
            'index': i,
            'selected': i == item.selected_index,
            **v.to_dict()
        })

    # Generate diff between first two different versions
    diff_lines = []
    diff_a_idx = 0
    diff_b_idx = 1
    if request.args.get('diff_a') is not None:
        diff_a_idx = int(request.args.get('diff_a', 0))
        diff_b_idx = int(request.args.get('diff_b', 1))

    # Side-by-side diff data
    side_by_side = []
    if len(item.versions) >= 2:
        diff_a_idx = min(diff_a_idx, len(item.versions) - 1)
        diff_b_idx = min(diff_b_idx, len(item.versions) - 1)
        engine = state['engine']
        diff_lines = engine.generate_diff(item.versions[diff_a_idx], item.versions[diff_b_idx])
        side_by_side = generate_side_by_side_diff(
            item.versions[diff_a_idx], item.versions[diff_b_idx])

    return render_template('conflict_detail.html',
                           filepath=filepath,
                           item=item,
                           versions=versions,
                           diff_lines=diff_lines,
                           side_by_side=side_by_side,
                           diff_a_idx=diff_a_idx,
                           diff_b_idx=diff_b_idx,
                           prev_path=prev_path,
                           next_path=next_path,
                           current_idx=current_idx + 1,
                           total_conflicts=len(conflict_paths),
                           resolved=item.resolved)


@app.route('/resolve', methods=['POST'])
def resolve():
    filepath = request.form.get('filepath', '')
    selected = int(request.form.get('selected_index', 0))
    inv = state['inventory']

    if filepath in inv:
        inv[filepath].selected_index = selected
        inv[filepath].resolved = True
        state['engine'].log('resolved', filepath,
                            inv[filepath].versions[selected].source_name)
        save_item_resolution(filepath, selected, True)

    next_path = request.form.get('next_path', '')
    if next_path:
        return redirect(url_for('conflict_detail', filepath=next_path))
    return redirect(url_for('conflicts'))


@app.route('/resolve-all-latest', methods=['POST'])
def resolve_all_latest():
    """Resolve all conflicts by selecting the latest version."""
    inv = state['inventory']
    count = 0
    for item in inv.values():
        if item.category == 'conflict' and not item.resolved:
            latest_idx = 0
            latest_time = 0
            for i, v in enumerate(item.versions):
                if v.modified_time > latest_time:
                    latest_time = v.modified_time
                    latest_idx = i
            item.selected_index = latest_idx
            item.resolved = True
            count += 1
            state['engine'].log('auto_resolved_latest', item.relative_path,
                                item.versions[latest_idx].source_name)

    # Batch-save all resolutions to SQLite
    updates = [(item.relative_path, item.selected_index, True)
               for item in inv.values()
               if item.category == 'conflict' and item.resolved]
    save_batch_resolutions(updates)
    _auto_save_state()
    flash(f'Auto-resolved {count} conflicts (selected latest version for each).', 'success')
    return redirect(url_for('conflicts'))


@app.route('/execute', methods=['POST'])
def execute():
    inv = state['inventory']
    target = state['target_dir']
    mode = request.form.get('mode', 'all')  # 'all', 'auto', 'resolved'

    if mode == 'auto':
        exec_inv = {k: v for k, v in inv.items()
                    if v.category in ('auto_unique', 'auto_identical')}
    elif mode == 'resolved':
        exec_inv = {k: v for k, v in inv.items()
                    if v.category == 'conflict' and v.resolved}
    else:
        exec_inv = {k: v for k, v in inv.items()
                    if v.category in ('auto_unique', 'auto_identical')
                    or (v.category == 'conflict' and v.resolved)}

    if not exec_inv:
        flash('No files to merge.', 'warning')
        return redirect(url_for('inventory'))

    if not target:
        flash('No target directory configured.', 'error')
        return redirect(url_for('setup'))

    os.makedirs(target, exist_ok=True)

    # Execute in background with progress queue
    state['merge_status'] = 'running'
    state['merge_result'] = None
    progress_queue = queue.Queue()
    state['merge_queue'] = progress_queue

    engine = state['engine']

    def run_merge():
        result = engine.execute_merge(exec_inv, target, progress_queue)
        state['merge_result'] = result
        state['merge_status'] = 'done'
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            f"merge_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        engine.save_log(log_path)
        state['merge_log_path'] = log_path

    thread = threading.Thread(target=run_merge, daemon=True)
    thread.start()

    return redirect(url_for('merge_progress'))


@app.route('/merge-progress')
def merge_progress():
    return render_template('merge_progress.html')


@app.route('/merge-events')
def merge_events():
    """SSE endpoint for merge progress."""
    def generate():
        q = state.get('merge_queue')
        if not q:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No merge running'})}\n\n"
            return

        while True:
            try:
                msg = q.get(timeout=1)
                if msg[0] == 'merge_start':
                    yield f"data: {json.dumps({'type': 'start', 'total': msg[1]})}\n\n"
                elif msg[0] == 'merge_progress':
                    yield f"data: {json.dumps({'type': 'progress', 'done': msg[1], 'total': msg[2]})}\n\n"
                elif msg[0] == 'merge_done':
                    result = state.get('merge_result', {})
                    yield f"data: {json.dumps({'type': 'done', 'new_files': msg[1], 'updated': msg[2], 'skipped': msg[3], 'errors': msg[4], 'new_file_list': result.get('new_file_list', []), 'updated_file_list': result.get('updated_file_list', [])})}\n\n"
                    return
            except queue.Empty:
                if state.get('merge_status') == 'done':
                    result = state.get('merge_result', {})
                    yield f"data: {json.dumps({'type': 'done', 'new_files': result.get('new_files', 0), 'updated': result.get('updated', 0), 'skipped': result.get('skipped', 0), 'errors': result.get('errors', 0), 'new_file_list': result.get('new_file_list', []), 'updated_file_list': result.get('updated_file_list', [])})}\n\n"
                    return
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/log')
def log_page():
    engine = state['engine']
    log_path = state.get('merge_log_path', '')
    return render_template('log.html',
                           log_entries=engine.log_entries[-200:],
                           suggestions=[],
                           merge_result=state.get('merge_result'),
                           log_path=log_path)


@app.route('/export-log')
def export_log():
    engine = state['engine']
    return Response(
        json.dumps(engine.log_entries, indent=2, ensure_ascii=False),
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment; filename=merge_log.json'}
    )


@app.route('/browse')
def file_browser():
    """Split-pane file browser — data loaded client-side via /api/browse-tree."""
    inv = state['inventory']
    if not inv:
        flash('No scan results. Please run a scan first.', 'warning')
        return redirect(url_for('setup'))
    # Detect if this is an agent review session
    sid = state.get('_session_id', '')
    agent_session_id = sid.replace('review_', '', 1) if sid.startswith('review_') else None
    return render_template('browse.html', agent_session_id=agent_session_id)


@app.route('/ide')
def ide_view():
    """VS Code-style IDE shell — single-page app with tabs, activity bar, command palette."""
    sid = state.get('_session_id', '')
    agent_session_id = sid.replace('review_', '', 1) if sid.startswith('review_') else None
    return render_template('ide_shell.html', agent_session_id=agent_session_id)


@app.route('/api/file-detail/<path:filepath>')
def file_detail_api(filepath):
    """Returns HTML fragment for the right-side detail panel."""
    inv = state['inventory']
    item = inv.get(filepath)
    if not item:
        return '<div class="text-muted p-4">File not found.</div>', 404

    versions = []
    max_size = max(v.file_size for v in item.versions) if item.versions else 0
    max_mtime = max(v.modified_time for v in item.versions) if item.versions else 0
    max_lines = max((v.line_count or 0) for v in item.versions) if item.versions else 0
    multi = len(item.versions) > 1
    for i, v in enumerate(item.versions):
        vd = v.to_dict()
        vd['index'] = i
        vd['selected'] = i == item.selected_index
        vd['is_largest'] = multi and v.file_size == max_size
        vd['is_latest'] = multi and v.modified_time == max_mtime
        vd['is_most_lines'] = multi and (v.line_count or 0) == max_lines and max_lines > 0
        versions.append(vd)

    # Generate diffs if 2+ versions
    side_by_side = []
    hunks = []
    diff_a_idx = int(request.args.get('diff_a', 0))
    diff_b_idx = int(request.args.get('diff_b', min(1, len(item.versions) - 1)))
    if len(item.versions) >= 2:
        diff_a_idx = min(diff_a_idx, len(item.versions) - 1)
        diff_b_idx = min(diff_b_idx, len(item.versions) - 1)
        side_by_side = generate_side_by_side_diff(item.versions[diff_a_idx], item.versions[diff_b_idx])
        hunks = _generate_merge_hunks(item.versions[diff_a_idx], item.versions[diff_b_idx])

    return render_template('_file_detail.html',
                           filepath=filepath,
                           item=item,
                           versions=versions,
                           side_by_side=side_by_side,
                           hunks=hunks,
                           diff_a_idx=diff_a_idx,
                           diff_b_idx=diff_b_idx,
                           num_versions=len(item.versions),
                           source_a=item.versions[diff_a_idx].source_name if len(item.versions) >= 2 else '',
                           source_b=item.versions[diff_b_idx].source_name if len(item.versions) >= 2 else '')


def _count_files_in_tree(node: dict) -> int:
    count = 0
    for value in node.values():
        if isinstance(value, MergeItem):
            count += 1
        elif isinstance(value, dict):
            count += _count_files_in_tree(value)
    return count


def _count_conflicts_in_tree(node: dict) -> int:
    count = 0
    for value in node.values():
        if isinstance(value, MergeItem) and value.category == 'conflict':
            count += 1
        elif isinstance(value, dict):
            count += _count_conflicts_in_tree(value)
    return count


def _browse_tree_from_agent_sessions():
    """Build a browse tree from agent session modified files when no merge inventory exists."""
    try:
        import agent_schema
        sessions = agent_schema.list_sessions()
    except Exception:
        return jsonify({'tree': {}, 'stats': {}})

    # Gather modified files from active (non-merged) sessions
    all_files = []
    for s in sessions:
        if s['status'] in ('merged', 'rejected'):
            continue
        files = agent_schema.get_session_files(s['session_id'])
        for f in files:
            if f.get('status') in ('modified', 'new'):
                all_files.append({
                    'relative_path': f['relative_path'],
                    'status': f['status'],
                    'lines_added': f.get('lines_added', 0),
                    'lines_removed': f.get('lines_removed', 0),
                    'session_id': s['session_id'],
                })

    if not all_files:
        return jsonify({'tree': {}, 'stats': {}})

    # Build tree in the same format as the merge tree
    tree = {}
    for f in all_files:
        parts = f['relative_path'].replace('\\', '/').split('/')
        node = tree
        for part in parts[:-1]:
            if part not in node:
                node[part] = {'_type': 'dir', 'file_count': 0, 'conflict_count': 0, 'children': {}}
            if node[part].get('_type') == 'dir':
                node = node[part]['children']
            else:
                break
        else:
            cat = 'conflict' if f['status'] == 'modified' else 'unique'
            node[parts[-1]] = {
                '_type': 'file',
                'relative_path': f['relative_path'],
                'category': cat,
                'resolved': False,
                'num_versions': 2 if f['status'] == 'modified' else 1,
                'versions': [],
                'selected_index': 0,
                'agent_session_id': f['session_id'],
                'lines_added': f['lines_added'],
                'lines_removed': f['lines_removed'],
            }

    # Compute directory counts
    def _update_counts(node):
        fc, cc = 0, 0
        for name, val in node.items():
            if isinstance(val, dict) and val.get('_type') == 'dir':
                cfc, ccc = _update_counts(val['children'])
                val['file_count'] = cfc
                val['conflict_count'] = ccc
                fc += cfc
                cc += ccc
            elif isinstance(val, dict) and val.get('_type') == 'file':
                fc += 1
                if val['category'] == 'conflict':
                    cc += 1
        return fc, cc

    total_files, total_conflicts = _update_counts(tree)
    stats = {
        'total': total_files,
        'auto_unique': total_files - total_conflicts,
        'auto_identical': 0,
        'conflicts': total_conflicts,
        'resolved': 0,
    }
    return jsonify({'tree': tree, 'stats': stats})


@app.route('/api/browse-tree')
def browse_tree_api():
    """Return the entire inventory as a nested JSON tree for client-side rendering."""
    inv = state['inventory']
    if not inv:
        # Fall back to agent session modified files
        return _browse_tree_from_agent_sessions()

    def _build_tree_node(node):
        """Recursively build a JSON-serializable tree."""
        result = {}
        for name, value in sorted(node.items()):
            if isinstance(value, MergeItem):
                item = value
                max_size = max(v.file_size for v in item.versions)
                max_mtime = max(v.modified_time for v in item.versions)
                max_lines = max((v.line_count or 0) for v in item.versions)
                multi = len(item.versions) > 1
                ver_list = []
                for vi, v in enumerate(item.versions):
                    ver_list.append({
                        'index': vi,
                        'source_name': v.source_name,
                        'modified': v.modified_dt().strftime('%Y-%m-%d %H:%M:%S'),
                        'size': v.size_human(),
                        'size_bytes': v.file_size,
                        'lines': v.line_count,
                        'sha_short': v.sha256[:8],
                        'selected': vi == item.selected_index,
                        'is_largest': multi and v.file_size == max_size,
                        'is_latest': multi and v.modified_time == max_mtime,
                        'is_most_lines': multi and (v.line_count or 0) == max_lines and max_lines > 0,
                    })
                cat = 'conflict' if item.category == 'conflict' else 'unique' if item.category == 'auto_unique' else 'identical'
                result[name] = {
                    '_type': 'file',
                    'relative_path': item.relative_path,
                    'category': cat,
                    'resolved': item.resolved,
                    'num_versions': len(item.versions),
                    'versions': ver_list,
                    'selected_index': item.selected_index,
                }
            elif isinstance(value, dict):
                subtree = _build_tree_node(value)
                file_count = _count_files_in_tree(value)
                conflict_count = _count_conflicts_in_tree(value)
                result[name] = {
                    '_type': 'dir',
                    'file_count': file_count,
                    'conflict_count': conflict_count,
                    'children': subtree,
                }
        return result

    # Build directory tree (same logic as file_browser)
    tree = {}
    for item in inv.values():
        parts = item.relative_path.split('/')
        node = tree
        for part in parts[:-1]:
            if part not in node:
                node[part] = {}
            node = node[part]
        node[parts[-1]] = item

    stats = _compute_stats(inv)
    return jsonify({'tree': _build_tree_node(tree), 'stats': stats})


@app.route('/api/repo-tree')
def repo_tree_api():
    """Return the full file tree of the target (original repo) directory."""
    target = state.get('target_dir', '')
    if not target or not os.path.isdir(target):
        return jsonify({'tree': {}, 'root': ''})

    ignore = state.get('ignore_patterns', DEFAULT_IGNORE)
    tree = {}
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in ignore]
        for fname in files:
            rel = os.path.relpath(os.path.join(root, fname), target).replace('\\', '/')
            parts = rel.split('/')
            node = tree
            for part in parts[:-1]:
                if part not in node:
                    node[part] = {'_type': 'dir', 'children': {}}
                node = node[part]['children']
            try:
                fpath = os.path.join(root, fname)
                st = os.stat(fpath)
                sz = st.st_size
                lc = None
                is_bin = FileScanner.detect_binary(fpath)
                if not is_bin and sz < 2_000_000:
                    try:
                        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                            lc = sum(1 for _ in f)
                    except Exception:
                        pass
                node[parts[-1]] = {
                    '_type': 'file',
                    'relative_path': rel,
                    'size': f"{sz / 1048576:.1f} MB" if sz > 1048576 else f"{sz / 1024:.1f} KB" if sz > 1024 else f"{sz} B",
                    'lines': lc,
                    'is_binary': is_bin,
                }
            except OSError:
                node[parts[-1]] = {'_type': 'file', 'relative_path': rel, 'size': '?', 'lines': None, 'is_binary': False}

    return jsonify({'tree': tree, 'root': target})


@app.route('/api/repo-file-content/<path:filepath>')
def repo_file_content_api(filepath):
    """Return raw content of a file from the target directory."""
    target = state.get('target_dir', '')
    if not target:
        return jsonify({'error': 'No target directory configured'}), 400

    resolved = os.path.realpath(os.path.join(target, filepath))
    base = os.path.realpath(target)
    if not resolved.startswith(base + os.sep) and resolved != base:
        return jsonify({'error': 'Path escapes target directory'}), 403

    if not os.path.isfile(resolved):
        return jsonify({'error': 'File not found'}), 404

    is_bin = FileScanner.detect_binary(resolved)
    if is_bin:
        return jsonify({'content': '', 'is_binary': True, 'lines': 0})

    try:
        enc = FileScanner.detect_encoding(resolved)
        with open(resolved, encoding=enc, errors='replace') as f:
            content = f.read()
        ext = os.path.splitext(filepath)[1].lower()
        lang_map = {
            '.py': 'python', '.js': 'javascript', '.jsx': 'jsx', '.ts': 'typescript',
            '.tsx': 'tsx', '.html': 'html', '.htm': 'html', '.css': 'css', '.scss': 'css',
            '.json': 'json', '.md': 'markdown', '.sql': 'sql', '.sh': 'shell',
            '.bash': 'shell', '.yaml': 'yaml', '.yml': 'yaml', '.xml': 'xml',
            '.toml': 'toml', '.rs': 'rust', '.go': 'go', '.java': 'java',
            '.c': 'c', '.cpp': 'cpp', '.h': 'cpp', '.rb': 'ruby', '.php': 'php',
        }
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        return jsonify({'content': content, 'is_binary': False, 'lines': content.count('\n') + 1, 'language': lang_map.get(ext, '')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/save-repo-file', methods=['POST'])
def save_repo_file():
    """Save edited file content back to the target directory."""
    data = request.get_json()
    filepath = data.get('filepath', '')
    content = data.get('content', '')

    if not filepath:
        return jsonify({'error': 'No filepath provided'}), 400

    target = state.get('target_dir', '')
    if not target:
        return jsonify({'error': 'No target directory configured'}), 400

    resolved = os.path.realpath(os.path.join(target, filepath))
    base = os.path.realpath(target)
    if not resolved.startswith(base + os.sep) and resolved != base:
        return jsonify({'error': 'Path escapes target directory'}), 403

    if not os.path.isfile(resolved):
        return jsonify({'error': 'File not found'}), 404

    try:
        with open(resolved, 'w', encoding='utf-8', newline='') as f:
            f.write(content)
        lines = content.count('\n') + (1 if content and not content.endswith('\n') else 0)
        return jsonify({'ok': True, 'lines': lines, 'size': os.path.getsize(resolved)})
    except (OSError, PermissionError) as e:
        return jsonify({'error': str(e)}), 500


_LANG_MAP = {
    '.py': 'python', '.js': 'javascript', '.jsx': 'jsx', '.ts': 'typescript',
    '.tsx': 'tsx', '.html': 'html', '.htm': 'html', '.css': 'css', '.scss': 'css',
    '.json': 'json', '.md': 'markdown', '.sql': 'sql', '.sh': 'shell',
    '.bash': 'shell', '.yaml': 'yaml', '.yml': 'yaml', '.xml': 'xml',
    '.toml': 'toml', '.rs': 'rust', '.go': 'go', '.java': 'java',
    '.c': 'c', '.cpp': 'cpp', '.h': 'cpp', '.rb': 'ruby', '.php': 'php',
}


@app.route('/api/inline-diff/<path:filepath>')
def inline_diff_api(filepath):
    """Return hunk data for inline diff review of a conflict/merge item."""
    inv = state['inventory']
    item = inv.get(filepath)
    if not item:
        return jsonify({'error': 'File not found in inventory'}), 404

    if len(item.versions) < 2:
        return jsonify({'error': 'File has only one version — nothing to diff'}), 400

    idx_a = int(request.args.get('a', 0))
    idx_b = int(request.args.get('b', item.selected_index if item.selected_index is not None else min(1, len(item.versions) - 1)))

    if idx_a >= len(item.versions) or idx_b >= len(item.versions):
        return jsonify({'error': 'Version index out of range'}), 400

    va, vb = item.versions[idx_a], item.versions[idx_b]
    hunks = _generate_merge_hunks(va, vb)

    ext = os.path.splitext(filepath)[1].lower()
    conflict_count = sum(1 for h in hunks if h.get('type') == 'conflict')

    return jsonify({
        'filepath': filepath,
        'source_a': va.source_name,
        'source_b': vb.source_name,
        'idx_a': idx_a,
        'idx_b': idx_b,
        'language': _LANG_MAP.get(ext, ''),
        'hunks': hunks,
        'conflict_count': conflict_count,
        'resolved': item.resolved,
    })


@app.route('/api/search')
def search_api():
    """Search across source files by content. Returns matching lines grouped by file."""
    import re as _re
    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify({'results': [], 'count': 0})

    target = state.get('target_dir', '')
    if not target or not os.path.isdir(target):
        return jsonify({'results': [], 'count': 0, 'error': 'No target directory'})

    results = []
    total = 0
    max_results = 200
    try:
        pattern = _re.compile(_re.escape(query), _re.IGNORECASE)
    except _re.error:
        return jsonify({'results': [], 'count': 0, 'error': 'Invalid pattern'})

    for root, dirs, files in os.walk(target):
        # Skip ignored dirs
        dirs[:] = [d for d in dirs if d not in DEFAULT_IGNORE]
        for fname in files:
            if total >= max_results:
                break
            ext = os.path.splitext(fname)[1].lower()
            if ext in BINARY_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, target).replace('\\', '/')
            try:
                with open(fpath, encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                matches = []
                for i, line in enumerate(lines):
                    if pattern.search(line):
                        matches.append({'line': i + 1, 'text': line.rstrip()[:200]})
                        total += 1
                        if total >= max_results:
                            break
                if matches:
                    results.append({'path': rel, 'matches': matches})
            except (OSError, PermissionError):
                continue
        if total >= max_results:
            break

    return jsonify({'results': results, 'count': total, 'truncated': total >= max_results})


@app.route('/resolve-accept-defaults', methods=['POST'])
def resolve_accept_defaults():
    """Mark unresolved conflicts as resolved using their current auto-selected version.
    Optional 'prefix' form field to limit to a specific folder path."""
    inv = state['inventory']
    prefix = request.form.get('prefix', '').strip()
    count = 0
    for item in inv.values():
        if item.category == 'conflict' and not item.resolved:
            if prefix and not item.relative_path.startswith(prefix + '/') and item.relative_path != prefix:
                continue
            item.resolved = True
            count += 1
            selected_name = item.versions[item.selected_index].source_name if item.versions else 'unknown'
            state['engine'].log('accept_default', item.relative_path, selected_name)
    # Batch-save resolutions to SQLite
    updates = [(item.relative_path, item.selected_index, True)
               for item in inv.values()
               if item.category == 'conflict' and item.resolved
               and (not prefix or item.relative_path.startswith(prefix + '/') or item.relative_path == prefix)]
    save_batch_resolutions(updates)
    _auto_save_state()
    return jsonify({'count': count, 'prefix': prefix or '(all)',
                     'message': f'Accepted defaults for {count} conflicts.'})


@app.route('/resolve-toggle', methods=['POST'])
def resolve_toggle():
    """Toggle resolved state for a single file (confirm/unconfirm)."""
    filepath = request.form.get('filepath', '')
    inv = state['inventory']
    if filepath in inv:
        item = inv[filepath]
        item.resolved = not item.resolved
        if item.resolved:
            state['engine'].log('confirmed', filepath, item.versions[item.selected_index].source_name)
        else:
            state['engine'].log('unconfirmed', filepath)
        save_item_resolution(filepath, item.selected_index, item.resolved)
        return jsonify({'resolved': item.resolved})
    return jsonify({'error': 'not found'}), 404


@app.route('/api/file-content/<path:filepath>')
def file_content(filepath):
    """API endpoint to view file content for a specific version."""
    inv = state['inventory']
    item = inv.get(filepath)
    if not item:
        return jsonify({'error': 'File not found'}), 404

    version_idx = int(request.args.get('version', 0))
    if version_idx >= len(item.versions):
        return jsonify({'error': 'Version not found'}), 404

    version = item.versions[version_idx]
    if version.is_binary:
        return jsonify({
            'content': '[Binary file - cannot display]',
            'source': version.source_name,
            'is_binary': True,
        })

    try:
        enc = FileScanner.detect_encoding(version.absolute_path)
        with open(version.absolute_path, encoding=enc, errors='replace') as f:
            content = f.read(500_000)  # limit to 500KB
    except (OSError, PermissionError) as e:
        return jsonify({'error': str(e)}), 500

    # Map file extension to syntax highlighting language identifier
    ext = os.path.splitext(filepath)[1].lower()
    lang_map = {
        '.py': 'python', '.js': 'javascript', '.jsx': 'jsx', '.ts': 'typescript',
        '.tsx': 'tsx', '.html': 'html', '.htm': 'html', '.css': 'css', '.scss': 'css',
        '.json': 'json', '.md': 'markdown', '.sql': 'sql', '.sh': 'shell',
        '.bash': 'shell', '.yaml': 'yaml', '.yml': 'yaml', '.xml': 'xml',
        '.toml': 'toml', '.rs': 'rust', '.go': 'go', '.java': 'java',
        '.c': 'cpp', '.cpp': 'cpp', '.h': 'cpp', '.rb': 'ruby', '.php': 'php',
    }

    content = content.replace('\r\n', '\n').replace('\r', '\n')
    return jsonify({
        'content': content,
        'source': version.source_name,
        'is_binary': False,
        'encoding': enc,
        'language': lang_map.get(ext, ''),
        'lines': content.count('\n') + (1 if content and not content.endswith('\n') else 0),
    })


# ---------------------------------------------------------------------------
# Interactive Merge Editor
# ---------------------------------------------------------------------------

def _generate_merge_hunks(version_a: FileVersion, version_b: FileVersion) -> list:
    """Generate merge hunks: list of {type, lines_a, lines_b, start_a, start_b}.
    type is 'equal' or 'conflict'. For equal hunks, lines_a == lines_b."""
    if version_a.is_binary or version_b.is_binary:
        return [{'type': 'binary', 'lines_a': [], 'lines_b': []}]

    try:
        enc_a = FileScanner.detect_encoding(version_a.absolute_path)
        enc_b = FileScanner.detect_encoding(version_b.absolute_path)
        with open(version_a.absolute_path, encoding=enc_a, errors='replace') as f:
            raw_a = f.readlines()
        with open(version_b.absolute_path, encoding=enc_b, errors='replace') as f:
            raw_b = f.readlines()
    except (OSError, PermissionError) as e:
        return [{'type': 'error', 'lines_a': [str(e)], 'lines_b': []}]

    max_lines = 15000
    raw_a = raw_a[:max_lines]
    raw_b = raw_b[:max_lines]

    lines_a = [l.rstrip('\n\r') for l in raw_a]
    lines_b = [l.rstrip('\n\r') for l in raw_b]

    matcher = difflib.SequenceMatcher(None, lines_a, lines_b)
    hunks = []
    hunk_id = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            hunks.append({
                'id': hunk_id,
                'type': 'equal',
                'lines_a': lines_a[i1:i2],
                'lines_b': lines_b[j1:j2],
                'start_a': i1 + 1,
                'start_b': j1 + 1,
            })
        else:
            hunks.append({
                'id': hunk_id,
                'type': 'conflict',
                'tag': tag,  # 'replace', 'insert', 'delete'
                'lines_a': lines_a[i1:i2],
                'lines_b': lines_b[j1:j2],
                'start_a': i1 + 1,
                'start_b': j1 + 1,
            })
        hunk_id += 1

    return hunks


@app.route('/merge/<path:filepath>')
def merge_editor(filepath):
    """Interactive merge editor page."""
    inv = state['inventory']
    item = inv.get(filepath)
    if not item or len(item.versions) < 2:
        flash('File not found or only has one version.', 'error')
        return redirect(url_for('conflicts'))

    idx_a = int(request.args.get('a', 0))
    idx_b = int(request.args.get('b', min(1, len(item.versions) - 1)))
    idx_a = min(idx_a, len(item.versions) - 1)
    idx_b = min(idx_b, len(item.versions) - 1)

    versions = []
    for i, v in enumerate(item.versions):
        versions.append({'index': i, 'source_name': v.source_name, **v.to_dict()})

    hunks = _generate_merge_hunks(item.versions[idx_a], item.versions[idx_b])

    return render_template('merge_editor.html',
                           filepath=filepath,
                           item=item,
                           versions=versions,
                           hunks=hunks,
                           idx_a=idx_a,
                           idx_b=idx_b,
                           source_a=item.versions[idx_a].source_name,
                           source_b=item.versions[idx_b].source_name)


@app.route('/api/save-merge', methods=['POST'])
def save_merge():
    """Save a manually merged file. Creates a new 'Merged' version."""
    data = request.get_json()
    filepath = data.get('filepath', '')
    content = data.get('content', '')
    inv = state['inventory']
    item = inv.get(filepath)

    if not item:
        return jsonify({'error': 'File not found'}), 404

    # Save merged file to session folder
    sid = state.get('_session_id', '')
    if not sid:
        return jsonify({'error': 'No active session'}), 400

    merged_dir = os.path.join(_session_dir(sid), 'merged')
    merged_path = os.path.join(merged_dir, filepath.replace('/', os.sep))
    os.makedirs(os.path.dirname(merged_path), exist_ok=True)

    with open(merged_path, 'w', encoding='utf-8', newline='') as f:
        f.write(content)

    # Compute metadata for the merged version
    stat = os.stat(merged_path)
    sha = FileScanner.compute_hash(merged_path)
    line_count = content.count('\n') + (1 if content and not content.endswith('\n') else 0)

    merged_version = FileVersion(
        source_name='Merged',
        source_root=merged_dir,
        absolute_path=merged_path,
        relative_path=filepath,
        file_size=stat.st_size,
        modified_time=time.time(),
        created_time=time.time(),
        sha256=sha,
        line_count=line_count,
        is_binary=False,
    )

    # Check if we already have a Merged version, replace it
    merged_idx = None
    for i, v in enumerate(item.versions):
        if v.source_name == 'Merged':
            merged_idx = i
            break

    if merged_idx is not None:
        item.versions[merged_idx] = merged_version
    else:
        merged_idx = len(item.versions)
        item.versions.append(merged_version)

    # Select the merged version and mark resolved
    item.selected_index = merged_idx
    item.resolved = True

    # Save to SQLite
    save_item_resolution(filepath, merged_idx, True)
    # Also update versions in DB
    conn = _get_db(sid)
    # Remove old versions for this file and re-insert all
    conn.execute('DELETE FROM file_versions WHERE relative_path=?', (filepath,))
    for vi, v in enumerate(item.versions):
        conn.execute(
            'INSERT INTO file_versions (relative_path, version_index, source_name, source_root, '
            'absolute_path, file_size, modified_time, created_time, sha256, line_count, is_binary) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (filepath, vi, v.source_name, v.source_root, v.absolute_path,
             v.file_size, v.modified_time, v.created_time, v.sha256,
             v.line_count, 1 if v.is_binary else 0)
        )
    conn.commit()

    state['engine'].log('merged', filepath, 'Merged',
                        f"Manual merge from {len(item.versions) - 1} versions")

    return jsonify({
        'ok': True,
        'message': f'Merged version saved ({line_count} lines, {merged_version.size_human()})',
    })


# ---------------------------------------------------------------------------
# Source Coverage
# ---------------------------------------------------------------------------

def _compute_coverage(inventory: dict) -> list:
    """Compute per-source coverage stats from the inventory."""
    source_stats = {}  # source_name -> {total, unique, identical, conflict, selected}
    for item in inventory.values():
        for v in item.versions:
            sn = v.source_name
            if sn not in source_stats:
                source_stats[sn] = {
                    'source_name': sn,
                    'total_files': 0,
                    'unique_files': 0,
                    'identical_files': 0,
                    'conflict_files': 0,
                    'selected_files': 0,
                }
            source_stats[sn]['total_files'] += 1
            if item.category == 'auto_unique':
                source_stats[sn]['unique_files'] += 1
            elif item.category == 'auto_identical':
                source_stats[sn]['identical_files'] += 1
            elif item.category == 'conflict':
                source_stats[sn]['conflict_files'] += 1
        # Count which source is selected
        sel = item.selected_version
        if sel and sel.source_name in source_stats:
            source_stats[sel.source_name]['selected_files'] += 1

    return sorted(source_stats.values(), key=lambda s: s['total_files'], reverse=True)


def _save_coverage_to_db(coverage: list):
    """Save coverage snapshot to SQLite."""
    sid = state.get('_session_id', '')
    if not sid:
        return
    conn = _get_db(sid)
    now = datetime.now().isoformat()
    conn.execute('DELETE FROM coverage')
    for c in coverage:
        conn.execute(
            'INSERT INTO coverage (source_name, total_files, unique_files, conflict_files, '
            'identical_files, snapshot_at) VALUES (?,?,?,?,?,?)',
            (c['source_name'], c['total_files'], c['unique_files'],
             c['conflict_files'], c['identical_files'], now)
        )
    conn.commit()


@app.route('/coverage')
def coverage_page():
    """Source coverage breakdown page."""
    inv = state['inventory']
    if not inv:
        flash('No scan results. Please run a scan first.', 'warning')
        return redirect(url_for('setup'))

    coverage = _compute_coverage(inv)
    stats = _compute_stats(inv)
    _save_coverage_to_db(coverage)

    return render_template('coverage.html', coverage=coverage, stats=stats)


@app.route('/api/coverage')
def coverage_api():
    """API endpoint for source coverage data."""
    inv = state['inventory']
    if not inv:
        return jsonify({'coverage': [], 'stats': {}})
    coverage = _compute_coverage(inv)
    stats = _compute_stats(inv)
    return jsonify({'coverage': coverage, 'stats': stats})


@app.route('/api/coverage-files/<source_name>')
def coverage_files_api(source_name):
    """Return files belonging to a specific source, grouped by category."""
    inv = state['inventory']
    files = {'unique': [], 'identical': [], 'conflict_selected': [], 'conflict_not_selected': []}
    for item in inv.values():
        source_names = [v.source_name for v in item.versions]
        if source_name not in source_names:
            continue
        sel = item.selected_version
        if item.category == 'auto_unique':
            files['unique'].append(item.relative_path)
        elif item.category == 'auto_identical':
            files['identical'].append(item.relative_path)
        elif item.category == 'conflict':
            if sel and sel.source_name == source_name:
                files['conflict_selected'].append(item.relative_path)
            else:
                files['conflict_not_selected'].append(item.relative_path)
    for k in files:
        files[k].sort()
    return jsonify(files)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    from logging.handlers import RotatingFileHandler

    # --- Log directory ---
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(_log_dir, exist_ok=True)

    # --- Formatters ---
    console_fmt = logging.Formatter(
        '%(asctime)s %(levelname)-5s [%(name)s] %(message)s', datefmt='%H:%M:%S')
    file_fmt = logging.Formatter(
        '%(asctime)s %(levelname)-5s [%(name)s] %(funcName)s:%(lineno)d  %(message)s')

    # --- Console handler (INFO) ---
    console_h = logging.StreamHandler()
    console_h.setLevel(logging.INFO)
    console_h.setFormatter(console_fmt)

    # --- File handler: everything (DEBUG) — rotates at 5 MB, keeps 3 backups ---
    file_h = RotatingFileHandler(
        os.path.join(_log_dir, 'guanine.log'),
        maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(file_fmt)

    # --- File handler: errors only — separate file for quick triage ---
    err_h = RotatingFileHandler(
        os.path.join(_log_dir, 'errors.log'),
        maxBytes=2 * 1024 * 1024, backupCount=2, encoding='utf-8')
    err_h.setLevel(logging.WARNING)
    err_h.setFormatter(file_fmt)

    # --- Root logger ---
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console_h)
    root.addHandler(file_h)
    root.addHandler(err_h)

    # Quiet noisy libraries
    logging.getLogger('werkzeug').setLevel(logging.INFO)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    print("=" * 60)
    print("  Guanine (CodeEdit) — Multi-Agent Orchestration Platform")
    print("  Open http://localhost:5000/ide in your browser")
    print(f"  Logs: {_log_dir}")
    print("=" * 60)
    app.run(debug=True, port=5000, threaded=True)
