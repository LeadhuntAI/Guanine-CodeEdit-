"""Generate an indented text representation of the repository file tree."""

from __future__ import annotations

import fnmatch
import json
import os

from spark.ignore import SKIP_DIRS, SKIP_EXTENSIONS, SKIP_FILES, SKIP_DIR_GLOBS


def execute(
    max_depth: int = 4,
    include_hidden: bool = False,
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """Walk directory tree and produce indented text. Returns JSON string."""
    try:
        base = os.path.realpath(_base_dir)
        lines = []
        total_files = 0
        total_dirs = 0

        def walk(dir_path: str, prefix: str, depth: int) -> None:
            nonlocal total_files, total_dirs

            if depth > max_depth:
                return

            try:
                entries = sorted(os.listdir(dir_path))
            except OSError:
                return

            dirs = []
            files = []
            for entry in entries:
                if not include_hidden and entry.startswith("."):
                    continue
                full = os.path.join(dir_path, entry)
                if os.path.isdir(full):
                    if entry not in SKIP_DIRS and not any(fnmatch.fnmatch(entry, g) for g in SKIP_DIR_GLOBS):
                        dirs.append(entry)
                else:
                    if entry not in SKIP_FILES:
                        ext = os.path.splitext(entry)[1]
                        if ext not in SKIP_EXTENSIONS:
                            files.append(entry)

            all_entries = [(d, True) for d in dirs] + [(f, False) for f in files]

            for i, (name, is_dir) in enumerate(all_entries):
                is_last = i == len(all_entries) - 1
                connector = "\u2514\u2500\u2500 " if is_last else "\u251c\u2500\u2500 "
                lines.append(f"{prefix}{connector}{name}{'/' if is_dir else ''}")

                if is_dir:
                    total_dirs += 1
                    extension = "    " if is_last else "\u2502   "
                    walk(os.path.join(dir_path, name), prefix + extension, depth + 1)
                else:
                    total_files += 1

        root_name = os.path.basename(base) or base
        lines.append(f"{root_name}/")
        walk(base, "", 1)

        return json.dumps({
            "tree": "\n".join(lines),
            "total_files": total_files,
            "total_dirs": total_dirs,
        })

    except Exception as exc:
        return json.dumps({"error": str(exc)})
