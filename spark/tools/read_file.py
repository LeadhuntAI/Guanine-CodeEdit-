"""Read file contents, optionally a line range."""

from __future__ import annotations

import json
import os

from spark.ignore import SKIP_DIRS


def _validate_path(path: str, base_dir: str) -> str | None:
    """Resolve path and ensure it stays within base_dir. Returns resolved path or None."""
    resolved = os.path.realpath(os.path.join(base_dir, path))
    base = os.path.realpath(base_dir)
    if not resolved.startswith(base + os.sep) and resolved != base:
        return None
    return resolved


def _in_skipped_dir(path: str) -> bool:
    """Check if any path component is a skipped directory."""
    parts = path.replace("\\", "/").split("/")
    return any(part in SKIP_DIRS for part in parts[:-1])


def execute(
    path: str,
    start_line: int = None,
    end_line: int = None,
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """Read file at _base_dir/path. Returns JSON string."""
    try:
        if _in_skipped_dir(path):
            return json.dumps({"error": f"Path is in a skipped directory: {path}"})

        resolved = _validate_path(path, _base_dir)
        if resolved is None:
            return json.dumps({"error": "Path escapes base directory"})

        if not os.path.isfile(resolved):
            return json.dumps({"error": f"File not found: {path}"})

        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        total = len(lines)

        if start_line is not None or end_line is not None:
            s = (start_line or 1) - 1  # convert to 0-based
            e = end_line or total
            s = max(0, s)
            e = min(total, e)
            lines = lines[s:e]

        content = "".join(lines)
        return json.dumps({"content": content, "lines": len(lines)})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
