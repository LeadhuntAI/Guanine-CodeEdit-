"""
This is your project's agentic engine. Customize as needed.

Tool execution and JSON extraction utilities.

Handles the messy reality of extracting structured data from LLM output
and dispatching tool calls safely with signature-aware argument filtering.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_json(response: str) -> dict | None:
    """Extract a JSON object from an LLM response string.

    Processing order:
    1. Strip ``<think>...</think>`` blocks (reasoning traces).
    2. Look for the last ````json ... ``` `` fenced block.
    3. Fall back to brace-matching to find the outermost ``{...}``.

    Returns the parsed dict, or *None* on failure.
    """
    if not response:
        return None

    # 1. Remove <think> blocks
    cleaned = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL)

    # 2. Try fenced ```json blocks — take the *last* one
    fenced = re.findall(r"```json\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced[-1].strip())
        except json.JSONDecodeError:
            pass

    # 3. Brace-matching fallback
    return _extract_braced_json(cleaned)


def _extract_braced_json(text: str) -> dict | None:
    """Find the last top-level ``{...}`` in *text* via brace counting."""
    # Walk backwards to find the last closing brace
    end = text.rfind("}")
    if end == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    start = -1

    for i in range(end, -1, -1):
        ch = text[i]

        if escape:
            escape = False
            continue

        if ch == "\\" and in_string:
            escape = True
            continue

        if ch == '"' and not escape:
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                start = i
                break

    if start == -1:
        return None

    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_tool_args(raw_args: str) -> dict:
    """Parse a raw string into a dict of tool arguments.

    Strategies (tried in order):
    1. ``json.loads`` — standard JSON
    2. Regex key-value extraction (``"key": "value"`` pairs)
    3. Brace-matching to isolate a JSON object, then parse
    """
    if not raw_args or not raw_args.strip():
        return {}

    raw_args = raw_args.strip()

    # Strategy 1: direct parse
    try:
        parsed = json.loads(raw_args)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Strategy 2: regex key-value pairs
    pairs = re.findall(
        r'"(\w+)"\s*:\s*("(?:[^"\\]|\\.)*"|\d+(?:\.\d+)?|true|false|null|\[.*?\]|\{.*?\})',
        raw_args,
        re.DOTALL,
    )
    if pairs:
        reconstructed = "{" + ", ".join(f'"{k}": {v}' for k, v in pairs) + "}"
        try:
            return json.loads(reconstructed)
        except json.JSONDecodeError:
            pass

    # Strategy 3: brace matching
    result = _extract_braced_json(raw_args)
    if result is not None:
        return result

    return {}


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool_call(
    available_tools: dict[str, Any],
    tool_name: str,
    tool_args: dict | str,
) -> str:
    """Dispatch a tool call and return the result as a JSON string.

    Parameters
    ----------
    available_tools : dict
        Mapping of ``{"tool_name": callable, ...}``.
    tool_name : str
        Name of the tool to invoke.
    tool_args : dict | str
        Arguments — parsed dict *or* raw JSON string.

    Returns
    -------
    str
        JSON-encoded result (always a string, never raises).
    """
    try:
        if tool_name not in available_tools:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        func = available_tools[tool_name]

        # Ensure args is a dict
        if isinstance(tool_args, str):
            args = parse_tool_args(tool_args)
        else:
            args = dict(tool_args) if tool_args else {}

        # Filter args to match the function's actual signature
        args = _filter_args(func, args)

        result = func(**args)

        # Ensure result is JSON-serialisable
        if isinstance(result, str):
            try:
                json.loads(result)
                return result  # already valid JSON string
            except (json.JSONDecodeError, TypeError):
                return json.dumps({"result": result})
        else:
            return json.dumps(result, default=str)

    except Exception as exc:
        logger.exception("Tool execution failed: %s", tool_name)
        return json.dumps({"error": str(exc)})


def _coerce_arg(value: Any, annotation: Any) -> Any:
    """Best-effort type coercion for LLM-provided arguments.

    LLMs frequently pass ``"3"`` (string) instead of ``3`` (int) or
    ``"true"`` instead of ``True``.  This function coerces the value
    to match the parameter's type annotation when possible.
    """
    if annotation is inspect.Parameter.empty or value is None:
        return value

    # Unwrap Optional[T] → T
    origin = getattr(annotation, "__origin__", None)
    if origin is Union:
        type_args = [a for a in annotation.__args__ if a is not type(None)]
        if type_args:
            annotation = type_args[0]
            origin = getattr(annotation, "__origin__", None)

    try:
        if annotation is int and isinstance(value, str):
            return int(value)
        if annotation is float and isinstance(value, str):
            return float(value)
        if annotation is bool and isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
    except (ValueError, TypeError):
        pass

    return value


def _filter_args(func: Any, args: dict) -> dict:
    """Keep only arguments that the function's signature accepts,
    and coerce types to match the function's annotations."""
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return args  # can't introspect → pass everything

    params = sig.parameters
    # If there's a **kwargs catch-all, pass everything through (but still coerce known params)
    has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    accepted = {
        name
        for name, p in params.items()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }

    filtered = {}
    for k, v in args.items():
        if k in accepted:
            param = params[k]
            filtered[k] = _coerce_arg(v, param.annotation)
        elif has_var_keyword:
            filtered[k] = v

    return filtered
