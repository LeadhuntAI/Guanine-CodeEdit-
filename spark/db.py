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


def _parse_md_sections(lines: list[str]) -> list[dict]:
    """Parse markdown into sections by heading boundaries.

    Returns a list of ``{heading, level, start_line, end_line}`` dicts.
    Line numbers are 0-based indices into *lines*.
    """
    sections: list[dict] = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            level = 0
            for ch in stripped:
                if ch == "#":
                    level += 1
                else:
                    break
            heading = stripped[level:].strip()
            if heading:
                sections.append({
                    "heading": heading,
                    "level": level,
                    "start_line": i,
                    "end_line": len(lines),  # updated below
                })

    # Set end_line for each section = start of next section at same or higher level
    for idx in range(len(sections) - 1):
        sections[idx]["end_line"] = sections[idx + 1]["start_line"]
    return sections


def _extract_file_paths(text: str, target_dir: str) -> list[str]:
    """Extract file paths mentioned in backticks that exist on disk."""
    import re as _re
    paths: set[str] = set()
    for m in _re.finditer(r'`([a-zA-Z0-9_./-]+\.\w+)`', text):
        candidate = m.group(1)
        if "/" in candidate and not candidate.startswith("http"):
            full = os.path.join(target_dir, candidate.replace("/", os.sep))
            if os.path.isfile(full):
                paths.add(candidate)
    return sorted(paths)


def _classify_file(path: str, language: str | None) -> str:
    """Classify a file into a category based on its path and language."""
    parts = path.replace("\\", "/").split("/")
    basename = parts[-1]
    ext = os.path.splitext(basename)[1].lower()

    # Documentation files (check before agentic — agentic/rules/docs/* is docs, not template)
    if any(p in ("rules", "docs") for p in parts):
        return "docs"

    # Note: agentic/ at root level in target repos is installed source code.
    # It only counts as "template" when under spark/templates/ or templates/
    # (handled by the templates/ path check below if needed).
    if basename in ("CLAUDE.md", "AGENTS.md", "README.md", "CHANGELOG.md"):
        return "docs"
    if basename.endswith(".md") and parts[0].startswith("."):
        return "docs"  # .claude/skills/*.md, .claude/RULES_INDEX.md, etc.

    # Config files
    _CONFIG_NAMES = {
        ".gitignore", "pyproject.toml", "setup.cfg", "setup.py",
        "requirements.txt", "Makefile", "Dockerfile",
        "docker-compose.yml", "docker-compose.yaml",
        ".flake8", ".eslintrc.json", ".prettierrc",
        "tsconfig.json", "package.json", "Cargo.toml",
        "go.mod", "pom.xml", "build.gradle",
    }
    if basename in _CONFIG_NAMES:
        return "config"
    if ext in (".json", ".toml", ".yaml", ".yml", ".ini", ".cfg") and len(parts) == 1:
        return "config"
    if basename.startswith("settings") or basename.startswith("spark_config"):
        return "config"

    # Scripts / utilities
    if ext in (".sh", ".bash", ".ps1", ".bat", ".cmd"):
        return "script"

    # UI templates
    if ext in (".html", ".htm", ".hbs", ".ejs", ".pug", ".svelte", ".vue"):
        return "ui"
    if ext in (".css", ".scss", ".less", ".sass"):
        return "ui"

    # Source code (known programming languages)
    if language and language not in ("markdown", "json", "yaml", "toml", "xml"):
        return "source"

    return "other"


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
                area_name TEXT,
                category TEXT DEFAULT 'other'
            );

            CREATE TABLE IF NOT EXISTS docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER REFERENCES files(id),
                area_name TEXT NOT NULL,
                doc_path TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                run_id INTEGER REFERENCES runs(id),
                source_hash TEXT,
                source_line_count INTEGER,
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

            CREATE TABLE IF NOT EXISTS area_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER REFERENCES runs(id),
                area_name TEXT NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                files_expected INTEGER DEFAULT 0,
                files_analyzed INTEGER DEFAULT 0,
                tool_errors INTEGER DEFAULT 0,
                stuck_loops INTEGER DEFAULT 0,
                doc_path TEXT,
                quality TEXT DEFAULT 'unknown',
                detail TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

        # --- Migrations for existing databases ---
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that may be missing in older databases."""
        docs_cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(docs)").fetchall()
        }
        if "source_line_count" not in docs_cols:
            self.conn.execute(
                "ALTER TABLE docs ADD COLUMN source_line_count INTEGER"
            )

        runs_cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "index_sha" not in runs_cols:
            self.conn.execute(
                "ALTER TABLE runs ADD COLUMN index_sha TEXT"
            )

        # Adopt mode: doc provenance and content hash tracking
        if "source" not in docs_cols:
            self.conn.execute(
                "ALTER TABLE docs ADD COLUMN source TEXT DEFAULT 'generated'"
            )
        if "doc_content_hash" not in docs_cols:
            self.conn.execute(
                "ALTER TABLE docs ADD COLUMN doc_content_hash TEXT"
            )

        files_cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(files)").fetchall()
        }
        if "category" not in files_cols:
            self.conn.execute(
                "ALTER TABLE files ADD COLUMN category TEXT DEFAULT 'other'"
            )
            # Backfill categories for existing rows
            rows = self.conn.execute("SELECT id, path, language FROM files").fetchall()
            for row in rows:
                cat = _classify_file(row[1], row[2])
                self.conn.execute(
                    "UPDATE files SET category = ? WHERE id = ?", (cat, row[0])
                )

        # --- Adopt mode tables ---
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS doc_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER REFERENCES docs(id),
                heading TEXT NOT NULL,
                heading_level INTEGER NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                covers_symbols TEXT,
                covers_files TEXT,
                last_verified_at TEXT
            );

            CREATE TABLE IF NOT EXISTS workflow_areas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                entry_points TEXT NOT NULL,
                symbol_chain TEXT NOT NULL,
                files TEXT NOT NULL,
                adopted_doc_id INTEGER REFERENCES docs(id),
                confidence REAL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                updated_at TEXT
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

    def save_index_sha(self, run_id: int, sha: str) -> None:
        """Store the git HEAD SHA at index time for symbol-level diffing."""
        self._exec(
            "UPDATE runs SET index_sha = ? WHERE id = ?",
            (sha, run_id),
        )

    def get_last_index_sha(self) -> Optional[str]:
        """Get the index SHA from the most recent completed run."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT index_sha FROM runs WHERE status = 'completed' AND index_sha IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            return row[0] if row else None

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
        category = _classify_file(path, language)
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO files (path, content_hash, last_scanned_at, language, line_count, category)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    last_scanned_at = excluded.last_scanned_at,
                    language = excluded.language,
                    line_count = excluded.line_count,
                    category = excluded.category
                """,
                (path, content_hash, now, language, line_count, category),
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
        source_line_count: int | None = None,
    ) -> None:
        self._exec(
            """
            INSERT INTO docs (file_id, area_name, doc_path, generated_at, run_id, source_hash, source_line_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (file_id, area_name, doc_path, _now_iso(), run_id, source_hash, source_line_count),
        )

    def delete_docs_by_path(self, doc_paths: list[str]) -> int:
        """Delete doc entries whose doc_path matches any of the given paths.

        Returns the number of rows deleted.
        """
        if not doc_paths:
            return 0
        with self._lock:
            placeholders = ",".join("?" for _ in doc_paths)
            cur = self.conn.execute(
                f"DELETE FROM docs WHERE doc_path IN ({placeholders})",
                doc_paths,
            )
            self.conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Template seeding
    # ------------------------------------------------------------------

    def seed_template_docs(
        self,
        target_dir: str,
        doc_map: dict[str, list[str]],
        self_documenting: list[str],
        instructions_file: str,
    ) -> int:
        """Seed DB with template files so fill-gaps/refresh modes skip them.

        Must be called AFTER onboarding (placeholder replacement is done).
        Returns the number of files seeded.
        """
        import logging

        logger = logging.getLogger(__name__)

        # Create a synthetic run to anchor the doc entries
        run_id = self.start_run(
            mode="template-seed", iterations=0, config_snapshot="{}"
        )

        seeded = 0

        # Helper: seed a single file as documented
        def _seed_file(rel_path: str, area_name: str, doc_path: str) -> bool:
            abs_path = os.path.join(target_dir, rel_path.replace("/", os.sep))
            if not os.path.isfile(abs_path):
                return False
            try:
                content_hash = _sha256(abs_path)
                language = _detect_language(abs_path)
                lines = _line_count(abs_path)
                # Normalise path to forward slashes for DB consistency
                norm_path = rel_path.replace(os.sep, "/")
                file_id = self.upsert_file(norm_path, content_hash, language, lines)
                self.record_doc(
                    file_id=file_id,
                    area_name=area_name,
                    doc_path=doc_path.replace(os.sep, "/"),
                    run_id=run_id,
                    source_hash=content_hash,
                    source_line_count=lines,
                )
                return True
            except Exception as exc:
                logger.debug("Failed to seed %s: %s", rel_path, exc)
                return False

        # 1. Files with pre-written documentation (agentic engine etc.)
        for doc_rel_path, source_files in doc_map.items():
            # Seed the doc file itself as self-documenting
            if _seed_file(doc_rel_path, "_template_docs", doc_rel_path):
                seeded += 1
            # Seed each source file, linked to the doc
            area_name = os.path.splitext(os.path.basename(doc_rel_path))[0]
            for src_path in source_files:
                if _seed_file(src_path, area_name, doc_rel_path):
                    seeded += 1

        # 2. Self-documenting files (rules, skills, config)
        for rel_path in self_documenting:
            if _seed_file(rel_path, "_template", rel_path):
                seeded += 1

        # 3. Instructions file (CLAUDE.md / AGENTS.md)
        if instructions_file:
            if _seed_file(instructions_file, "_template", instructions_file):
                seeded += 1

        self.complete_run(run_id)
        return seeded

    def import_existing_docs(self, target_dir: str, platform_dir: str = ".claude") -> int:
        """Scan for existing doc files and import them into the DB.

        Called on first run when the repo already has Agent Blueprint docs
        but no spark.db history. Finds all ``*.md`` files in
        ``{platform_dir}/rules/docs/`` and registers them so fill-gaps/refresh
        modes recognise them as documented.

        Returns the number of doc files imported.
        """
        import logging
        import glob as globmod

        logger = logging.getLogger(__name__)
        docs_dir = os.path.join(target_dir, platform_dir, "rules", "docs")
        if not os.path.isdir(docs_dir):
            return 0

        doc_files = globmod.glob(os.path.join(docs_dir, "*.md"))
        if not doc_files:
            return 0

        # Create a synthetic run for the imports
        run_id = self.start_run(
            mode="import-existing", iterations=0, config_snapshot="{}"
        )
        imported = 0

        for doc_file in doc_files:
            basename = os.path.basename(doc_file)
            if basename.startswith("."):
                continue
            area_name = os.path.splitext(basename)[0]
            doc_rel = f"{platform_dir}/rules/docs/{basename}".replace(os.sep, "/")

            # Read the doc to find which files it covers.
            # Look for file paths mentioned in tables or code blocks.
            covered_paths: list[str] = []
            try:
                with open(doc_file, "r", encoding="utf-8") as fh:
                    import re as _re
                    for line in fh:
                        # Match backtick-wrapped file paths like `src/foo/bar.py`
                        for m in _re.finditer(r'`([a-zA-Z0-9_./-]+\.\w+)`', line):
                            candidate = m.group(1)
                            # Must look like a real file path (has directory separator)
                            if "/" in candidate and not candidate.startswith("http"):
                                full = os.path.join(target_dir, candidate.replace("/", os.sep))
                                if os.path.isfile(full):
                                    covered_paths.append(candidate)
            except OSError:
                pass

            # Deduplicate
            covered_paths = sorted(set(covered_paths))

            if not covered_paths:
                # At minimum, seed the doc file itself
                doc_hash = _sha256(doc_file)
                doc_lang = _detect_language(doc_file)
                doc_lines = _line_count(doc_file)
                file_id = self.upsert_file(doc_rel, doc_hash, doc_lang, doc_lines)
                self.record_doc(
                    file_id=file_id, area_name=area_name,
                    doc_path=doc_rel, run_id=run_id, source_hash=doc_hash,
                    source_line_count=doc_lines,
                )
                imported += 1
                continue

            # Seed each covered file
            for rel_path in covered_paths:
                norm_path = rel_path.replace(os.sep, "/")
                abs_path = os.path.join(target_dir, rel_path.replace("/", os.sep))
                try:
                    content_hash = _sha256(abs_path)
                    language = _detect_language(abs_path)
                    lines = _line_count(abs_path)
                    file_id = self.upsert_file(norm_path, content_hash, language, lines)
                    self.record_doc(
                        file_id=file_id, area_name=area_name,
                        doc_path=doc_rel, run_id=run_id, source_hash=content_hash,
                        source_line_count=lines,
                    )
                except Exception as exc:
                    logger.debug("Failed to import %s: %s", rel_path, exc)
            imported += 1

        self.complete_run(run_id)
        return imported

    # ------------------------------------------------------------------
    # Adopt mode
    # ------------------------------------------------------------------

    def adopt_existing_docs(
        self, target_dir: str, platform_dir: str = ".claude"
    ) -> list[dict]:
        """Import existing hand-crafted docs with section-level tracking.

        Unlike ``import_existing_docs`` (which does basic file-path matching),
        this method parses each doc's markdown structure into sections, stores
        them in ``doc_sections``, and marks docs as ``source='adopted'``.

        Returns a list of ``{doc_path, area_name, sections}`` dicts for
        downstream workflow analysis.
        """
        import logging
        import glob as globmod

        logger = logging.getLogger(__name__)
        docs_dir = os.path.join(target_dir, platform_dir, "rules", "docs")
        if not os.path.isdir(docs_dir):
            return []

        doc_files = globmod.glob(os.path.join(docs_dir, "*.md"))
        if not doc_files:
            return []

        run_id = self.start_run(
            mode="adopt", iterations=0, config_snapshot="{}"
        )
        results: list[dict] = []

        for doc_file in doc_files:
            basename = os.path.basename(doc_file)
            if basename.startswith("."):
                continue
            area_name = os.path.splitext(basename)[0]
            doc_rel = f"{platform_dir}/rules/docs/{basename}".replace(os.sep, "/")

            # Read and hash the doc content
            try:
                with open(doc_file, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()
            except OSError:
                continue

            doc_content = "".join(lines)
            doc_content_hash = hashlib.sha256(
                doc_content.encode("utf-8")
            ).hexdigest()

            # Parse sections from markdown headings
            sections = _parse_md_sections(lines)

            # Extract file paths mentioned in the doc (backtick-wrapped)
            covered_paths = _extract_file_paths(doc_content, target_dir)

            # Register each covered source file and create doc links
            if covered_paths:
                for rel_path in covered_paths:
                    abs_path = os.path.join(
                        target_dir, rel_path.replace("/", os.sep)
                    )
                    try:
                        content_hash = _sha256(abs_path)
                        language = _detect_language(abs_path)
                        lc = _line_count(abs_path)
                        file_id = self.upsert_file(
                            rel_path, content_hash, language, lc
                        )
                        self._exec(
                            """
                            INSERT INTO docs
                            (file_id, area_name, doc_path, generated_at, run_id,
                             source_hash, source_line_count, source, doc_content_hash)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'adopted', ?)
                            """,
                            (
                                file_id, area_name, doc_rel, _now_iso(),
                                run_id, content_hash, lc, doc_content_hash,
                            ),
                        )
                    except Exception as exc:
                        logger.debug("Failed to adopt %s: %s", rel_path, exc)
            else:
                # No file paths found — still register the doc itself
                doc_hash = _sha256(doc_file)
                doc_lang = _detect_language(doc_file)
                doc_lc = _line_count(doc_file)
                file_id = self.upsert_file(doc_rel, doc_hash, doc_lang, doc_lc)
                self._exec(
                    """
                    INSERT INTO docs
                    (file_id, area_name, doc_path, generated_at, run_id,
                     source_hash, source_line_count, source, doc_content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'adopted', ?)
                    """,
                    (
                        file_id, area_name, doc_rel, _now_iso(),
                        run_id, doc_hash, doc_lc, doc_content_hash,
                    ),
                )

            # Store sections in doc_sections table
            # Get the doc_id (pick any row we just inserted for this area)
            with self._lock:
                row = self.conn.execute(
                    "SELECT id FROM docs WHERE area_name = ? AND run_id = ? LIMIT 1",
                    (area_name, run_id),
                ).fetchone()
            doc_id = row[0] if row else None

            if doc_id and sections:
                for sec in sections:
                    sec_content = "".join(lines[sec["start_line"]:sec["end_line"]])
                    sec_hash = hashlib.sha256(
                        sec_content.encode("utf-8")
                    ).hexdigest()
                    # Extract file paths mentioned in this specific section
                    sec_files = _extract_file_paths(sec_content, target_dir)
                    self._exec(
                        """
                        INSERT INTO doc_sections
                        (doc_id, heading, heading_level, start_line, end_line,
                         content_hash, covers_files, last_verified_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            doc_id, sec["heading"], sec["level"],
                            sec["start_line"], sec["end_line"],
                            sec_hash,
                            json.dumps(sec_files) if sec_files else None,
                            _now_iso(),
                        ),
                    )

            results.append({
                "doc_path": doc_rel,
                "area_name": area_name,
                "sections": sections,
                "covered_files": covered_paths,
            })

        self.complete_run(run_id)
        return results

    def has_adopted_docs(self) -> bool:
        """Check if any adopted docs exist in the database."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM docs WHERE source = 'adopted'"
            ).fetchone()
            return row[0] > 0 if row else False

    def get_adopted_docs(self) -> list[dict]:
        """Return all adopted docs with their section counts."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT DISTINCT d.area_name, d.doc_path, d.doc_content_hash,
                       d.generated_at,
                       (SELECT COUNT(*) FROM doc_sections ds WHERE ds.doc_id = d.id) as section_count
                FROM docs d
                WHERE d.source = 'adopted'
                ORDER BY d.area_name
                """
            ).fetchall()
            return [
                {
                    "area_name": r[0],
                    "doc_path": r[1],
                    "doc_content_hash": r[2],
                    "generated_at": r[3],
                    "section_count": r[4],
                }
                for r in rows
            ]

    def get_doc_sections(self, area_name: str) -> list[dict]:
        """Return all sections for an adopted doc by area name."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT ds.id, ds.heading, ds.heading_level, ds.start_line, ds.end_line,
                       ds.content_hash, ds.covers_symbols, ds.covers_files
                FROM doc_sections ds
                JOIN docs d ON d.id = ds.doc_id
                WHERE d.area_name = ?
                ORDER BY ds.start_line
                """,
                (area_name,),
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "heading": r[1],
                    "heading_level": r[2],
                    "start_line": r[3],
                    "end_line": r[4],
                    "content_hash": r[5],
                    "covers_symbols": json.loads(r[6]) if r[6] else [],
                    "covers_files": json.loads(r[7]) if r[7] else [],
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Area results
    # ------------------------------------------------------------------

    def record_area_result(
        self,
        run_id: int,
        area_name: str,
        phase: str,
        status: str,
        files_expected: int = 0,
        files_analyzed: int = 0,
        tool_errors: int = 0,
        stuck_loops: int = 0,
        doc_path: Optional[str] = None,
        quality: str = "unknown",
        detail: Optional[str] = None,
    ) -> int:
        """Record the outcome of an area's exploration or doc-writing phase.

        Quality values: 'complete', 'partial', 'failed', 'unknown'.
        """
        cur = self._exec(
            """
            INSERT INTO area_results
                (run_id, area_name, phase, status, files_expected, files_analyzed,
                 tool_errors, stuck_loops, doc_path, quality, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, area_name, phase, status, files_expected, files_analyzed,
             tool_errors, stuck_loops, doc_path, quality, detail, _now_iso()),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def get_incomplete_areas(self) -> list[dict]:
        """Return areas from the most recent run that have quality != 'complete'.

        When multiple iterations exist, only considers the latest result
        per area+phase (highest id). This prevents earlier iteration
        failures from triggering unnecessary retries if a later iteration
        succeeded.

        Used by fill-gaps mode to identify areas that need re-exploration.
        """
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT ar.area_name, ar.phase, ar.quality, ar.status,
                       ar.files_expected, ar.files_analyzed, ar.tool_errors,
                       ar.stuck_loops, ar.detail
                FROM area_results ar
                JOIN (SELECT MAX(run_id) as last_run FROM area_results) lr
                  ON ar.run_id = lr.last_run
                WHERE ar.quality != 'complete'
                  AND ar.id IN (
                      SELECT MAX(id) FROM area_results
                      WHERE run_id = lr.last_run
                      GROUP BY area_name, phase
                  )
                ORDER BY ar.area_name, ar.phase
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_area_results_for_run(self, run_id: int) -> list[dict]:
        """Return area results for a given run, one per area+phase.

        When multiple iterations produce results for the same area+phase,
        keep only the latest (highest id) row. This prevents earlier
        iteration failures from masking later successes in the report.
        """
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT area_name, phase, status, quality,
                       files_expected, files_analyzed, tool_errors, stuck_loops,
                       doc_path, detail
                FROM area_results
                WHERE run_id = ?
                  AND id IN (
                      SELECT MAX(id) FROM area_results
                      WHERE run_id = ?
                      GROUP BY area_name, phase
                  )
                ORDER BY area_name, phase
                """,
                (run_id, run_id),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

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

        # Remove files from DB that are now in excluded/skipped directories
        self._prune_excluded_files(root, gitignore_patterns, exclude_patterns)

        return count

    def _prune_excluded_files(
        self,
        root: Path,
        gitignore_patterns: list[str],
        exclude_patterns: list[str],
    ) -> int:
        """Remove DB entries for files that should now be skipped.

        Catches files indexed before skip patterns were updated
        (e.g., .code-index files indexed before .code-index was added to SKIP_DIRS).
        """
        all_files = self.get_all_files()
        to_remove: list[int] = []
        for f in all_files:
            rel_path = f["path"]
            rel_parts = rel_path.split("/")
            if _should_skip(rel_path, rel_parts, gitignore_patterns, exclude_patterns):
                to_remove.append(f["id"])
        if to_remove:
            with self._lock:
                placeholders = ",".join("?" for _ in to_remove)
                self.conn.execute(
                    f"DELETE FROM docs WHERE file_id IN ({placeholders})", to_remove,
                )
                self.conn.execute(
                    f"DELETE FROM files WHERE id IN ({placeholders})", to_remove,
                )
                self.conn.commit()
        return len(to_remove)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self.conn.close()
