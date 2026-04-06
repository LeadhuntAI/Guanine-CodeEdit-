"""
Spark SQLite database layer.

Manages the `.claude/spark.db` database that tracks runs, scanned files,
generated documentation, iteration state, area plans, and relationship maps.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Language detection from file extension
# ---------------------------------------------------------------------------

_EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".java": "java",
    ".kt": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".m": "objective-c",
    ".scala": "scala",
    ".r": "r",
    ".R": "r",
    ".lua": "lua",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".md": "markdown",
    ".rst": "restructuredtext",
    ".vue": "vue",
    ".svelte": "svelte",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".clj": "clojure",
    ".jl": "julia",
    ".zig": "zig",
    ".nim": "nim",
    ".tf": "terraform",
    ".proto": "protobuf",
    ".graphql": "graphql",
    ".gql": "graphql",
}

# ---------------------------------------------------------------------------
# Patterns to always skip during file scanning
# Central source of truth: spark/ignore.py
# ---------------------------------------------------------------------------

from spark.ignore import SKIP_DIRS, SKIP_EXTENSIONS, SKIP_FILES, SKIP_FILE_PATTERNS

_ALWAYS_SKIP_DIRS = SKIP_DIRS
_ALWAYS_SKIP_EXTENSIONS = SKIP_EXTENSIONS
_ALWAYS_SKIP_FILES = SKIP_FILES


def _detect_language(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1]
    return _EXTENSION_TO_LANGUAGE.get(ext)


def _sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _line_count(filepath: str) -> int:
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_gitignore(target_dir: str) -> list[str]:
    """Return a list of simple gitignore-style patterns from .gitignore."""
    gitignore = Path(target_dir) / ".gitignore"
    patterns: list[str] = []
    if not gitignore.is_file():
        return patterns
    try:
        with open(gitignore, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    except OSError:
        pass
    return patterns


def _should_skip(
    rel_path: str,
    rel_parts: list[str],
    gitignore_patterns: list[str],
    exclude_patterns: list[str] | None = None,
) -> bool:
    """Decide whether to skip a file path based on ignore rules."""
    # Check directory components against always-skip set
    for part in rel_parts[:-1]:
        if part in _ALWAYS_SKIP_DIRS:
            return True

    filename = rel_parts[-1] if rel_parts else ""

    # Always-skip files
    if filename in _ALWAYS_SKIP_FILES:
        return True

    # Always-skip file patterns (e.g. .min.js, .bundle.js)
    for pattern in SKIP_FILE_PATTERNS:
        if filename.endswith(pattern):
            return True

    # Always-skip extensions
    ext = os.path.splitext(filename)[1]
    if ext in _ALWAYS_SKIP_EXTENSIONS:
        return True

    # User-specified exclude patterns
    if exclude_patterns:
        for pattern in exclude_patterns:
            clean = pattern.rstrip("/")
            # Directory pattern: "tests/" or "vendor/legacy"
            if pattern.endswith("/") or "/" not in pattern:
                for part in rel_parts[:-1]:
                    if part == clean:
                        return True
                # Also match if any path segment matches
                if clean in rel_parts:
                    return True
            # Path prefix: "vendor/legacy" matches "vendor/legacy/foo.py"
            if "/" in clean and rel_path.startswith(clean + "/"):
                return True
            if "/" in clean and rel_path.startswith(clean):
                return True
            # Glob extension pattern like "*.log"
            if pattern.startswith("*."):
                if filename.endswith(pattern[1:]):
                    return True
            # Exact match
            if rel_path == clean:
                return True

    # Gitignore patterns (simple matching)
    for pattern in gitignore_patterns:
        clean = pattern.rstrip("/")
        # Directory pattern like "node_modules/"
        if pattern.endswith("/"):
            for part in rel_parts[:-1]:
                if part == clean:
                    return True
            # Also match if the final component equals the dir pattern
            if filename == clean:
                return True
        # Glob extension pattern like "*.pyc"
        elif pattern.startswith("*."):
            if filename.endswith(pattern[1:]):
                return True
        # Exact filename or directory name match
        else:
            if filename == clean:
                return True
            for part in rel_parts[:-1]:
                if part == clean:
                    return True
    return False


def _row_to_dict(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class Database:
    """SQLite wrapper for the Spark tracking database."""

    def __init__(self, target_dir: str) -> None:
        db_path = Path(target_dir).resolve() / ".claude" / "spark.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _exec(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """Thread-safe execute + commit."""
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                mode TEXT NOT NULL,
                iterations_planned INTEGER,
                iterations_completed INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                config_snapshot TEXT
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                content_hash TEXT,
                last_scanned_at TEXT,
                language TEXT,
                line_count INTEGER,
                area_name TEXT
            );

            CREATE TABLE IF NOT EXISTS docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER REFERENCES files(id),
                area_name TEXT NOT NULL,
                doc_path TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                run_id INTEGER REFERENCES runs(id),
                source_hash TEXT,
                status TEXT DEFAULT 'current'
            );

            CREATE TABLE IF NOT EXISTS iteration_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER REFERENCES runs(id),
                iteration INTEGER NOT NULL,
                phase TEXT NOT NULL,
                area_name TEXT,
                input_json TEXT,
                output_json TEXT,
                started_at TEXT,
                completed_at TEXT,
                status TEXT DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS area_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER REFERENCES runs(id),
                iteration INTEGER NOT NULL,
                plan_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS relationship_maps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER REFERENCES runs(id),
                iteration INTEGER NOT NULL,
                map_json TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def start_run(self, mode: str, iterations: int, config_snapshot: str) -> int:
        cur = self._exec(
            "INSERT INTO runs (started_at, mode, iterations_planned, config_snapshot) VALUES (?, ?, ?, ?)",
            (_now_iso(), mode, iterations, config_snapshot),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def complete_run(self, run_id: int, status: str = "completed") -> None:
        self._exec(
            "UPDATE runs SET completed_at = ?, status = ? WHERE id = ?",
            (_now_iso(), status, run_id),
        )

    def update_run_iterations(self, run_id: int, iterations_completed: int) -> None:
        self._exec(
            "UPDATE runs SET iterations_completed = ? WHERE id = ?",
            (iterations_completed, run_id),
        )

    def get_last_run(self) -> Optional[dict]:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            if row is None:
                return None
            return _row_to_dict(cur, row)

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def upsert_file(
        self,
        path: str,
        content_hash: str,
        language: Optional[str],
        line_count: int,
    ) -> int:
        now = _now_iso()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO files (path, content_hash, last_scanned_at, language, line_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    last_scanned_at = excluded.last_scanned_at,
                    language = excluded.language,
                    line_count = excluded.line_count
                """,
                (path, content_hash, now, language, line_count),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
            return row[0]  # type: ignore[index]

    def get_file(self, path: str) -> Optional[dict]:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM files WHERE path = ?", (path,))
            row = cur.fetchone()
            if row is None:
                return None
            return _row_to_dict(cur, row)

    def get_all_files(self) -> list[dict]:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM files ORDER BY path")
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def get_documented_files(self) -> set[str]:
        """Return paths of files that have at least one doc with status='current'."""
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT DISTINCT f.path
                FROM docs d
                JOIN files f ON f.id = d.file_id
                WHERE d.status = 'current'
                """
            )
            return {row[0] for row in cur.fetchall()}

    def get_stale_files(self) -> set[str]:
        """Return paths where the file's current hash differs from the doc's source_hash."""
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT DISTINCT f.path
                FROM docs d
                JOIN files f ON f.id = d.file_id
                WHERE d.status = 'current'
                  AND f.content_hash != d.source_hash
                """
            )
            return {row[0] for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # Docs
    # ------------------------------------------------------------------

    def record_doc(
        self,
        file_id: int,
        area_name: str,
        doc_path: str,
        run_id: int,
        source_hash: str,
    ) -> None:
        self._exec(
            """
            INSERT INTO docs (file_id, area_name, doc_path, generated_at, run_id, source_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_id, area_name, doc_path, _now_iso(), run_id, source_hash),
        )

    def mark_docs_stale(self, file_paths: list[str]) -> None:
        if not file_paths:
            return
        placeholders = ",".join("?" for _ in file_paths)
        self._exec(
            f"""
            UPDATE docs SET status = 'stale'
            WHERE file_id IN (
                SELECT id FROM files WHERE path IN ({placeholders})
            ) AND status = 'current'
            """,
            file_paths,
        )

    # ------------------------------------------------------------------
    # Iteration state
    # ------------------------------------------------------------------

    def save_iteration_state(
        self,
        run_id: int,
        iteration: int,
        phase: str,
        area_name: Optional[str],
        input_json: Optional[str],
        status: str = "pending",
    ) -> int:
        cur = self._exec(
            """
            INSERT INTO iteration_state (run_id, iteration, phase, area_name, input_json, started_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, iteration, phase, area_name, input_json, _now_iso(), status),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def update_iteration_state(
        self,
        state_id: int,
        output_json: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        updates: list[str] = []
        params: list[object] = []
        if output_json is not None:
            updates.append("output_json = ?")
            params.append(output_json)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
            if status in ("completed", "failed"):
                updates.append("completed_at = ?")
                params.append(_now_iso())
        if not updates:
            return
        params.append(state_id)
        self._exec(
            f"UPDATE iteration_state SET {', '.join(updates)} WHERE id = ?",
            params,
        )

    def get_incomplete_states(self, run_id: int) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM iteration_state WHERE run_id = ? AND status != 'completed' ORDER BY id",
                (run_id,),
            )
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Area plans
    # ------------------------------------------------------------------

    def save_area_plan(self, run_id: int, iteration: int, plan_json: str) -> None:
        self._exec(
            "INSERT INTO area_plans (run_id, iteration, plan_json) VALUES (?, ?, ?)",
            (run_id, iteration, plan_json),
        )

    def get_latest_area_plan(self, run_id: int) -> Optional[dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM area_plans WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _row_to_dict(cur, row)

    # ------------------------------------------------------------------
    # Relationship maps
    # ------------------------------------------------------------------

    def save_relationship_map(self, run_id: int, iteration: int, map_json: str) -> None:
        self._exec(
            "INSERT INTO relationship_maps (run_id, iteration, map_json) VALUES (?, ?, ?)",
            (run_id, iteration, map_json),
        )

    def get_latest_relationship_map(self, run_id: int) -> Optional[dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM relationship_maps WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return _row_to_dict(cur, row)

    # ------------------------------------------------------------------
    # File scanning
    # ------------------------------------------------------------------

    def scan_files(self, target_dir: str, exclude_patterns: list[str] | None = None) -> int:
        """Walk *target_dir*, hash every eligible file, and upsert into the DB.

        Args:
            target_dir: Root directory to scan.
            exclude_patterns: User-specified patterns to exclude (folder names, paths, globs).

        Returns the number of files indexed.
        """
        root = Path(target_dir).resolve()
        gitignore_patterns = _parse_gitignore(str(root))
        exclude_patterns = exclude_patterns or []
        count = 0

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune directories in-place so os.walk doesn't descend into them
            rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
            dirnames[:] = [
                d
                for d in dirnames
                if d not in _ALWAYS_SKIP_DIRS
                and not _should_skip(
                    d, [d], gitignore_patterns, exclude_patterns
                )
            ]

            for fname in filenames:
                abs_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abs_path, root).replace("\\", "/")
                rel_parts = rel_path.split("/")

                if _should_skip(rel_path, rel_parts, gitignore_patterns, exclude_patterns):
                    continue

                try:
                    content_hash = _sha256(abs_path)
                    lines = _line_count(abs_path)
                except OSError:
                    continue

                lang = _detect_language(fname)
                self.upsert_file(rel_path, content_hash, lang, lines)
                count += 1

        return count

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self.conn.close()
