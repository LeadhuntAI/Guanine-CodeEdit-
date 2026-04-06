"""Search the Spark library catalog by keyword."""

import json

from spark.tools.list_library import _load_catalog


def execute(query: str = "", **kwargs) -> str:
    """Search the library catalog by keyword.

    Matches against name, description, tags, and category (case-insensitive).
    Returns matching plugin summaries.
    """
    if not query:
        return json.dumps({"error": "query is required", "plugins": [], "count": 0})

    try:
        catalog = _load_catalog()
    except (OSError, json.JSONDecodeError) as exc:
        return json.dumps({"error": f"Could not load catalog: {exc}"})

    q = query.lower()
    matches = []
    for p in catalog:
        searchable = " ".join([
            p.get("name", ""),
            p.get("description", ""),
            p.get("category", ""),
            " ".join(p.get("tags", [])),
        ]).lower()
        if q in searchable:
            matches.append({
                "id": p["id"],
                "name": p["name"],
                "description": p["description"],
                "category": p.get("category", ""),
                "tags": p.get("tags", []),
            })

    return json.dumps({"query": query, "plugins": matches, "count": len(matches)})
