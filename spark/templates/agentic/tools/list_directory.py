"""List directory entries, optionally recursive up to depth 3."""

from __future__ import annotations

import json
import os


def _validate_path(path: str, base_dir: str) -> str | None:
    """Resolve path and ensure it stays within base_dir. Returns resolved path or None."""
    resolved = os.path.realpath(os.path.join(base_dir, path))
    base = os.path.realpath(base_dir)
    if not resolved.startswith(base + os.sep) and resolved != base:
        return None
    return resolved


def _list_entries(dir_path: str, base_dir: str, recursive: bool, depth: int, max_depth: int) -> list[dict]:
    """List entries in a directory. For recursive, go up to max_depth."""
    entries = []
    try:
        items = sorted(os.listdir(dir_path))
    except OSError:
        return entries

    for item in items:
        full = os.path.join(dir_path, item)
        rel = os.path.relpath(full, base_dir).replace("\\", "/")
        is_dir = os.path.isdir(full)
        entries.append({"name": rel, "type": "dir" if is_dir else "file"})

        if recursive and is_dir and depth < max_depth:
            entries.extend(_list_entries(full, base_dir, True, depth + 1, max_depth))

    return entries


def execute(
    path: str = ".",
    recursive: bool = False,
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """List entries at _base_dir/path. Returns JSON string."""
    try:
        resolved = _validate_path(path, _base_dir)
        if resolved is None:
            return json.dumps({"error": "Path escapes base directory"})

        if not os.path.isdir(resolved):
            return json.dumps({"error": f"Not a directory: {path}"})

        entries = _list_entries(resolved, _base_dir, recursive, depth=0, max_depth=3)
        return json.dumps({"entries": entries, "count": len(entries)})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
