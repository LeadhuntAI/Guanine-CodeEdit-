"""Get full details for a specific Spark library plugin."""

import json

from spark.tools.list_library import _load_catalog


def execute(plugin_id: str = "", **kwargs) -> str:
    """Get the complete catalog entry for a plugin, including pros, cons,
    config options, and integration info.

    This is what the library agent uses to discuss trade-offs with the user.
    """
    if not plugin_id:
        return json.dumps({"error": "plugin_id is required"})

    try:
        catalog = _load_catalog()
    except (OSError, json.JSONDecodeError) as exc:
        return json.dumps({"error": f"Could not load catalog: {exc}"})

    for plugin in catalog:
        if plugin.get("id") == plugin_id:
            return json.dumps(plugin)

    return json.dumps({"error": f"Plugin not found: {plugin_id}"})
