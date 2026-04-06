"""
Tool registry: loads definitions.json, maps tool names to implementations,
and dispatches calls with injected _base_dir / _db arguments.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from spark.tools import read_file, write_file, list_directory
from spark.tools import search_code, get_file_tree, get_db_state, update_rules_index
from spark.tools import ask_user, read_spark_plans, install_templates, scan_existing
from spark.tools import list_library, search_library, get_plugin_details, install_plugin
from spark.tools import code_search, code_index

logger = logging.getLogger(__name__)

# Hard-coded map of tool name -> module with an execute() function
_TOOL_MODULES = {
    "read_file": read_file,
    "write_file": write_file,
    "list_directory": list_directory,
    "search_code": search_code,
    "get_file_tree": get_file_tree,
    "get_db_state": get_db_state,
    "update_rules_index": update_rules_index,
    "ask_user": ask_user,
    "read_spark_plans": read_spark_plans,
    "install_templates": install_templates,
    "scan_existing": scan_existing,
    "list_library": list_library,
    "search_library": search_library,
    "get_plugin_details": get_plugin_details,
    "install_plugin": install_plugin,
    "code_search": code_search,
    "code_index": code_index,
}


class ToolRegistry:
    """Loads tool definitions from JSON and dispatches calls to implementations."""

    def __init__(self, definitions_path: str, base_dir: str, db: Any = None) -> None:
        """
        Load tool definitions from JSON, resolve function references.

        Parameters
        ----------
        definitions_path : str
            Path to definitions.json.
        base_dir : str
            The target repo root (all file paths are relative to this).
        db : optional
            Database instance (for get_db_state tool).
        """
        self.base_dir = str(Path(base_dir).resolve())
        self.db = db

        with open(definitions_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Index definitions by name
        self._definitions: dict[str, dict] = {}
        for tool_def in data.get("tools", []):
            self._definitions[tool_def["name"]] = tool_def

        self._tool_sets: dict[str, list[str]] = data.get("tool_sets", {})

    def get_tools_for_role(self, tool_names: list[str]) -> list[dict]:
        """Return OpenAI-format tool schemas for the given tool names."""
        tools = []
        for name in tool_names:
            defn = self._definitions.get(name)
            if defn is None:
                logger.warning("Unknown tool requested: %s", name)
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": defn["name"],
                    "description": defn["description"],
                    "parameters": defn["parameters_schema"],
                },
            })
        return tools

    def get_tool_callables(self, tool_names: list[str]) -> dict[str, Any]:
        """Return {tool_name: callable} for use with execute_tool_call."""
        callables = {}
        for name in tool_names:
            if name in _TOOL_MODULES:
                module = _TOOL_MODULES[name]
                # Wrap to inject _base_dir and _db
                callables[name] = self._make_wrapper(name, module.execute)
        return callables

    def execute(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool by name with the given arguments. Returns JSON string."""
        try:
            module = _TOOL_MODULES.get(tool_name)
            if module is None:
                return json.dumps({"error": f"Unknown tool: {tool_name}"})

            kwargs = dict(arguments)
            kwargs["_base_dir"] = self.base_dir
            if self.db is not None:
                kwargs["_db"] = self.db

            return module.execute(**kwargs)
        except Exception as exc:
            logger.exception("Tool execution failed: %s", tool_name)
            return json.dumps({"error": str(exc)})

    def get_tool_set(self, set_name: str) -> list[str]:
        """Return tool names for a predefined tool set."""
        return list(self._tool_sets.get(set_name, []))

    def _make_wrapper(self, tool_name: str, func: Any) -> Any:
        """Create a wrapper that injects _base_dir and _db into calls."""
        base_dir = self.base_dir
        db = self.db

        def wrapper(**kwargs: Any) -> str:
            kwargs["_base_dir"] = base_dir
            if db is not None:
                kwargs["_db"] = db
            return func(**kwargs)

        # Preserve the original signature for introspection by tool_executor._filter_args
        import functools
        functools.update_wrapper(wrapper, func)
        return wrapper
