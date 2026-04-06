"""Code index tool — triggers jcodemunch indexing and checks status."""

from __future__ import annotations

import json
import os


def execute(
    action: str = "status",
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """Index the repository or check index status.

    Actions:
      index  — create or incrementally update the code index
      status — check if index exists and return stats
    """
    from spark.code_index import index_repo, load_index, get_repo_identifier, _INDEX_DIR_NAME

    try:
        if action == "index":
            result = index_repo(_base_dir)
            if result is None:
                return json.dumps({"error": "Indexing failed or jcodemunch not available"})
            return json.dumps({"status": "indexed", "details": result})

        elif action == "status":
            index_path = os.path.join(_base_dir, _INDEX_DIR_NAME)
            if not os.path.isdir(index_path):
                return json.dumps({"indexed": False, "message": "No code index found"})

            index = load_index(_base_dir)
            if index is None:
                return json.dumps({"indexed": False, "message": "Index directory exists but could not load"})

            owner, name = get_repo_identifier(_base_dir)
            return json.dumps({
                "indexed": True,
                "owner": owner,
                "name": name,
                "index_path": index_path,
            })

        else:
            return json.dumps({"error": f"Unknown action: {action}. Use: index, status"})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
