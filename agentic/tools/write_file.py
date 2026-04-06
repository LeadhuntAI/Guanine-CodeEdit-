"""Write content to a file, creating parent directories as needed."""

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


def execute(
    path: str,
    content: str,
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """Write content to _base_dir/path. Returns JSON string."""
    try:
        resolved = _validate_path(path, _base_dir)
        if resolved is None:
            return json.dumps({"error": "Path escapes base directory"})

        os.makedirs(os.path.dirname(resolved), exist_ok=True)

        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return json.dumps({"written": True, "path": path, "lines": line_count})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
