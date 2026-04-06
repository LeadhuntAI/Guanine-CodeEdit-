"""
Central ignore patterns for Spark and jCodeMunch.

Both Spark's file scanner (db.py) and jCodeMunch's indexer (index_folder)
should skip the same files and directories. This module is the single source
of truth — edit patterns here, not in db.py or security.py.

jCodeMunch has its own built-in skip list (security.py) that we can't modify
(vendored, overwritten on update). code_index.py computes the delta and passes
it as extra_ignore_patterns so the effective skip sets match.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Directories to always skip (matched by exact directory name)
# ---------------------------------------------------------------------------

SKIP_DIRS: set[str] = {
    # Version control
    ".git",
    # Package managers / deps
    "node_modules",
    "vendor",
    # Python
    "__pycache__",
    ".tox",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".egg-info",
    # Build output
    "dist",
    "build",
    "target",
    "generated",
    # JS frameworks
    ".next",
    ".nuxt",
    # JVM
    ".gradle",
    # Apple / Swift
    "DerivedData",
    ".build",
    # Data / fixtures (not source code)
    "test_data",
    "testdata",
    "fixtures",
    "snapshots",
    "migrations",
    "proto",
    # Spark itself (engine, vendors, templates — not user code)
    "spark",
}


# ---------------------------------------------------------------------------
# File extensions to always skip (binary / compiled)
# ---------------------------------------------------------------------------

SKIP_EXTENSIONS: set[str] = {
    ".pyc",
    ".pyo",
    ".class",
    ".o",
    ".so",
    ".dll",
    ".exe",
    ".bin",
    ".whl",
    ".egg",
    ".lock",
}


# ---------------------------------------------------------------------------
# Specific filenames to always skip
# ---------------------------------------------------------------------------

SKIP_FILES: set[str] = {
    ".env",
    ".env.local",
    ".DS_Store",
    "Thumbs.db",
    "package-lock.json",
    "yarn.lock",
    "go.sum",
}


# ---------------------------------------------------------------------------
# File patterns to skip (substring match against filename)
# ---------------------------------------------------------------------------

SKIP_FILE_PATTERNS: set[str] = {
    ".min.js",
    ".min.ts",
    ".bundle.js",
}


# ---------------------------------------------------------------------------
# Directory glob patterns (e.g. *.xcodeproj)
# ---------------------------------------------------------------------------

SKIP_DIR_GLOBS: set[str] = {
    "*.xcodeproj",
    "*.xcworkspace",
}


# ---------------------------------------------------------------------------
# jCodeMunch delta — patterns Spark skips that jCodeMunch doesn't have built-in.
# Passed as extra_ignore_patterns to index_folder().
#
# jCodeMunch built-ins (from security.py _SKIP_DIRECTORY_NAMES):
#   node_modules, vendor, venv, .venv, __pycache__, dist, build, .git,
#   .tox, .mypy_cache, target, .gradle, test_data, testdata, fixtures,
#   snapshots, migrations, generated, proto, DerivedData, .build
# ---------------------------------------------------------------------------

_JCODEMUNCH_BUILTIN_DIRS: set[str] = {
    "node_modules", "vendor", "venv", ".venv", "__pycache__",
    "dist", "build", ".git", ".tox", ".mypy_cache", "target",
    ".gradle", "test_data", "testdata", "fixtures", "snapshots",
    "migrations", "generated", "proto", "DerivedData", ".build",
}

JCODEMUNCH_EXTRA_IGNORE: list[str] = sorted(
    f"{d}/" for d in (SKIP_DIRS - _JCODEMUNCH_BUILTIN_DIRS)
)
