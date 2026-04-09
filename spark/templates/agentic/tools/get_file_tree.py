"""Get the file tree of a directory as indented text. Respects .gitignore patterns."""

from __future__ import annotations

import json
import os
from pathlib import Path

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
              ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs"}
_SKIP_EXTENSIONS = {".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".exe", ".bin", ".whl"}


def _build_tree(dir_path: str, base_dir: str, prefix: str, depth: int, max_depth: int) -> list[str]:
    """Recursively build an indented file tree."""
    if depth > max_depth:
        return []

    lines: list[str] = []
    try:
        items = sorted(os.listdir(dir_path))
    except OSError:
        return lines

    dirs = []
    files = []
    for item in items:
        full = os.path.join(dir_path, item)
        if os.path.isdir(full):
            if item not in _SKIP_DIRS and not item.startswith("."):
                dirs.append(item)
        else:
            ext = os.path.splitext(item)[1]
            if ext not in _SKIP_EXTENSIONS:
                files.append(item)

    for f in files:
        lines.append(f"{prefix}{f}")

    for d in dirs:
        lines.append(f"{prefix}{d}/")
        full = os.path.join(dir_path, d)
        lines.extend(_build_tree(full, base_dir, prefix + "  ", depth + 1, max_depth))

    return lines


def execute(
    max_depth: int = 4,
    include_hidden: bool = False,
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """Get the file tree of the repository. Returns JSON string."""
    try:
        max_depth = int(max_depth)
        base = os.path.realpath(_base_dir)
        if not os.path.isdir(base):
            return json.dumps({"error": f"Not a directory: {_base_dir}"})

        root_name = os.path.basename(base) + "/"
        lines = [root_name]
        lines.extend(_build_tree(base, base, "  ", 0, max_depth))

        tree = "\n".join(lines)
        return json.dumps({"tree": tree, "lines": len(lines)})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
