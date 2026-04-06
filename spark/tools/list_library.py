"""List plugins from the Spark library catalog."""

import json
import os


def _load_catalog() -> list[dict]:
    """Load the library catalog from spark/library/catalog.json."""
    catalog_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "library", "catalog.json",
    )
    with open(catalog_path, "r", encoding="utf-8") as f:
        return json.load(f)


def execute(category: str = "", **kwargs) -> str:
    """List all plugins in the Spark library. Optionally filter by category.

    Returns a summary list (id, name, description, category, tags) to
    keep token usage low — use get_plugin_details for the full entry.
    """
    try:
        catalog = _load_catalog()
    except (OSError, json.JSONDecodeError) as exc:
        return json.dumps({"error": f"Could not load catalog: {exc}"})

    if category:
        catalog = [p for p in catalog if p.get("category", "").lower() == category.lower()]

    summaries = [
        {
            "id": p["id"],
            "name": p["name"],
            "description": p["description"],
            "category": p.get("category", ""),
            "tags": p.get("tags", []),
        }
        for p in catalog
    ]

    return json.dumps({"plugins": summaries, "count": len(summaries)})
