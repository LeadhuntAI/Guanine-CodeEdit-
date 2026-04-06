"""Regex search in files under a directory."""

from __future__ import annotations

import fnmatch
import json
import os
import re

_MAX_MATCHES = 100

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache"}
_SKIP_EXTENSIONS = {".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".exe", ".bin", ".whl"}


def _validate_path(path: str, base_dir: str) -> str | None:
    resolved = os.path.realpath(os.path.join(base_dir, path))
    base = os.path.realpath(base_dir)
    if not resolved.startswith(base + os.sep) and resolved != base:
        return None
    return resolved


def execute(
    pattern: str,
    file_pattern: str = None,
    path: str = ".",
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """Search for regex pattern in files. Returns JSON string."""
    try:
        resolved = _validate_path(path, _base_dir)
        if resolved is None:
            return json.dumps({"error": "Path escapes base directory"})

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return json.dumps({"error": f"Invalid regex: {e}"})

        matches = []

        for dirpath, dirnames, filenames in os.walk(resolved):
            # Prune skipped directories
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

            for fname in filenames:
                ext = os.path.splitext(fname)[1]
                if ext in _SKIP_EXTENSIONS:
                    continue

                if file_pattern and not fnmatch.fnmatch(fname, file_pattern):
                    continue

                fpath = os.path.join(dirpath, fname)
                rel = os.path.relpath(fpath, _base_dir).replace("\\", "/")

                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for line_num, line in enumerate(f, 1):
                            if regex.search(line):
                                matches.append({
                                    "file": rel,
                                    "line": line_num,
                                    "content": line.rstrip("\n\r"),
                                })
                                if len(matches) >= _MAX_MATCHES:
                                    return json.dumps({
                                        "matches": matches,
                                        "count": len(matches),
                                        "truncated": True,
                                    })
                except (OSError, UnicodeDecodeError):
                    continue

        return json.dumps({"matches": matches, "count": len(matches)})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
