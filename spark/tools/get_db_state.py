"""Query the Spark database for file states."""

from __future__ import annotations

import json


def execute(
    query: str,
    _db=None,
    **kwargs,
) -> str:
    """Query the database. Returns JSON string."""
    try:
        if _db is None:
            return json.dumps({"error": "No database connection available"})

        if query == "all_files":
            files = _db.get_all_files()
            return json.dumps({"files": files, "count": len(files)})

        elif query == "documented_files":
            paths = _db.get_documented_files()
            result = sorted(paths)
            return json.dumps({"files": result, "count": len(result)})

        elif query == "stale_files":
            paths = _db.get_stale_files()
            result = sorted(paths)
            return json.dumps({"files": result, "count": len(result)})

        elif query == "undocumented_files":
            all_files = _db.get_all_files()
            documented = _db.get_documented_files()
            undocumented = [f["path"] for f in all_files if f["path"] not in documented]
            undocumented.sort()
            return json.dumps({"files": undocumented, "count": len(undocumented)})

        else:
            return json.dumps({"error": f"Unknown query: {query}. Valid: documented_files, stale_files, all_files, undocumented_files"})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
