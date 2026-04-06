"""
Agentic loop — ReAct text-parsing and native tool-calling modes.

Both loop variants share the same system-message assembly and result format
so callers can switch modes transparently.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from spark.engine.tool_executor import execute_tool_call
from spark.ui import ui

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ITERATIONS = 20

_REACT_FORMAT_INSTRUCTIONS = """\
You must respond using EXACTLY one of these two formats:

**To use a tool:**
Thought: <your reasoning about what to do next>
Action: <tool_name>
Action Input: <JSON object with the tool's arguments>
<END_OF_ACTION>

**To give your final answer:**
Thought: <your reasoning>
Answer: <your complete final answer>

IMPORTANT:
- Always start with "Thought:"
- Use "Action:" + "Action Input:" when you need to call a tool
- Use "Answer:" when you have the final result
- Never combine both in one response
"""


# ---------------------------------------------------------------------------
# System message assembly (shared)
# ---------------------------------------------------------------------------

def _build_system_message(
    layer_def: dict,
    knowledge: dict,
    tool_descriptions: str,
    mode: str = "react",
) -> str:
    """Assemble the full system prompt from layer definition + knowledge."""
    parts: list[str] = []

    # Base system prompt from layer definition
    base = layer_def.get("system_message") or layer_def.get("prompt", "")
    if base:
        parts.append(base)

    # Knowledge injections
    rules_text = knowledge.get("rules_text", "")
    if rules_text:
        parts.append(rules_text)

    rules_index = knowledge.get("rules_index", "")
    if rules_index:
        parts.append(rules_index)

    skills_index = knowledge.get("skills_index", "")
    if skills_index:
        parts.append(skills_index)

    # Tool descriptions
    if tool_descriptions:
        parts.append(f"<AVAILABLE_TOOLS>\n{tool_descriptions}\n</AVAILABLE_TOOLS>")

    # Format instructions (ReAct only — native mode relies on the API)
    if mode == "react":
        parts.append(_REACT_FORMAT_INSTRUCTIONS)

    return "\n\n".join(parts)


def _tool_descriptions_text(tool_registry: dict) -> str:
    """Build a human-readable list of tool names + docstrings."""
    lines: list[str] = []
    for name, func in tool_registry.items():
        doc = getattr(func, "__doc__", "") or ""
        doc = doc.strip().split("\n")[0]  # first line only
        lines.append(f"- {name}: {doc}")
    return "\n".join(lines)


def _tool_schemas(tool_registry: dict) -> list[dict]:
    """Build OpenAI-format tool schemas from the registry for native mode."""
    import inspect

    schemas: list[dict] = []
    for name, func in tool_registry.items():
        doc = (getattr(func, "__doc__", "") or "").strip()

        # If the function carries a 'schema' attribute, use it directly
        if hasattr(func, "tool_schema"):
            schemas.append(func.tool_schema)
            continue

        # Auto-generate a minimal schema from the signature
        params: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        try:
            sig = inspect.signature(func)
            for pname, p in sig.parameters.items():
                params["properties"][pname] = {"type": "string", "description": ""}
                if p.default is inspect.Parameter.empty:
                    params["required"].append(pname)
        except (ValueError, TypeError):
            pass

        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": doc[:200],
                    "parameters": params,
                },
            }
        )
    return schemas


# ---------------------------------------------------------------------------
# ReAct loop
# ---------------------------------------------------------------------------

def process_agentic_loop(
    layer_def: dict,
    session: Any,
    client: Any,
    tool_registry: dict,
    knowledge: dict,
) -> dict:
    """Run a ReAct-style agentic loop (text-parsed tool calls).

    Parameters
    ----------
    layer_def : dict
        Layer configuration — expects keys like ``system_message``, ``model``,
        ``max_iterations``, ``user_message``.
    session : LightweightWorkflowSession
    client : OpenRouterClient
    tool_registry : dict
        ``{"tool_name": callable, ...}``
    knowledge : dict
        Output of ``resolve_knowledge()``.

    Returns
    -------
    dict with ``response``, ``chat_history``, ``summary``.
    """
    max_iter = layer_def.get("max_iterations", _DEFAULT_MAX_ITERATIONS)
    model = layer_def.get("model", "openai/gpt-4o-mini")

    tool_desc = _tool_descriptions_text(tool_registry)
    system_msg = _build_system_message(layer_def, knowledge, tool_desc, mode="react")

    messages: list[dict] = [{"role": "system", "content": system_msg}]

    # Initial user message
    user_msg = layer_def.get("user_message", "")
    if user_msg:
        messages.append({"role": "user", "content": user_msg})
    elif session.chat_history:
        # Continue from existing history
        messages.extend(session.chat_history)

    final_answer = ""

    for iteration in range(max_iter):
        logger.debug("ReAct iteration %d/%d", iteration + 1, max_iter)
        ui.llm_start(model)
        try:
            resp = client.chat_completion(
                model=model,
                messages=messages,
                max_tokens=layer_def.get("max_tokens", 4096),
                temperature=layer_def.get("temperature", 0.3),
                stop=["<END_OF_ACTION>"],
            )
        except Exception as exc:
            ui.llm_done()
            logger.error("LLM call failed in ReAct loop: %s", exc)
            final_answer = f"Error: LLM call failed — {exc}"
            break

        ui.llm_done(resp.get("usage"))
        content = resp.get("content") or ""
        messages.append({"role": "assistant", "content": content})

        # Check for final answer
        answer_match = re.search(r"Answer:\s*(.*)", content, re.DOTALL)
        if answer_match:
            final_answer = answer_match.group(1).strip()
            break

        # Check for tool call
        action_match = re.search(r"Action:\s*(.+)", content)
        input_match = re.search(r"Action Input:\s*(.*)", content, re.DOTALL)

        if action_match:
            tool_name = action_match.group(1).strip()
            tool_args = input_match.group(1).strip() if input_match else "{}"

            ui.tool_call(tool_name)
            observation = execute_tool_call(tool_registry, tool_name, tool_args)

            messages.append(
                {"role": "user", "content": f"Observation: {observation}"}
            )
        else:
            # No action and no answer — treat the content as the final answer
            final_answer = content
            break
    else:
        # Loop exhausted without explicit answer
        final_answer = content if messages else "Max iterations reached without answer."

    chat_history = messages[1:]  # exclude system message
    summary = _safe_summarize(chat_history, client, model)

    return {
        "response": final_answer,
        "chat_history": chat_history,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Native tool-calling loop
# ---------------------------------------------------------------------------

def process_agentic_loop_native(
    layer_def: dict,
    session: Any,
    client: Any,
    tool_registry: dict,
    knowledge: dict,
) -> dict:
    """Run an agentic loop using native OpenAI-format function calling.

    Same interface and return format as :func:`process_agentic_loop`.
    """
    max_iter = layer_def.get("max_iterations", _DEFAULT_MAX_ITERATIONS)
    model = layer_def.get("model", "openai/gpt-4o-mini")

    tool_desc = _tool_descriptions_text(tool_registry)
    system_msg = _build_system_message(layer_def, knowledge, tool_desc, mode="native")

    messages: list[dict] = [{"role": "system", "content": system_msg}]

    user_msg = layer_def.get("user_message", "")
    if user_msg:
        messages.append({"role": "user", "content": user_msg})
    elif session.chat_history:
        messages.extend(session.chat_history)

    tools = _tool_schemas(tool_registry)
    final_answer = ""
    last_content = ""  # track the last non-empty content for fallback

    for iteration in range(max_iter):
        logger.debug("Native loop iteration %d/%d", iteration + 1, max_iter)
        ui.llm_start(model)
        try:
            resp = client.chat_completion(
                model=model,
                messages=messages,
                tools=tools if tools else None,
                max_tokens=layer_def.get("max_tokens", 4096),
                temperature=layer_def.get("temperature", 0.3),
            )
        except Exception as exc:
            ui.llm_done()
            logger.error("LLM call failed in native loop: %s", exc)
            final_answer = f"Error: LLM call failed — {exc}"
            break

        ui.llm_done(resp.get("usage"))
        content = resp.get("content")
        tool_calls = resp.get("tool_calls")

        if content:
            last_content = content

        if tool_calls:
            # Append assistant message with tool calls
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if content:
                assistant_msg["content"] = content
            assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            # Execute each tool call and append results
            for tc in tool_calls:
                tc_id = tc.get("id", "")
                func_info = tc.get("function", {})
                tool_name = func_info.get("name", "")
                tool_args = func_info.get("arguments", "{}")

                ui.tool_call(tool_name, tool_args[:80])
                observation = execute_tool_call(tool_registry, tool_name, tool_args)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": observation,
                    }
                )
        elif content:
            # No tool calls, just content — we're done
            messages.append({"role": "assistant", "content": content})
            final_answer = content
            break
        else:
            # Empty response
            final_answer = ""
            break
    else:
        # Loop exhausted — use the last content seen, or a fallback message
        final_answer = last_content or "Max iterations reached without final answer."
        logger.warning("Native loop exhausted %d iterations", max_iter)

    chat_history = messages[1:]
    summary = _safe_summarize(chat_history, client, model)

    return {
        "response": final_answer,
        "chat_history": chat_history,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Summarisation
# ---------------------------------------------------------------------------

def summarize_loop(
    chat_history: list[dict],
    client: Any,
    model: str,
) -> str:
    """Ask the LLM to produce a structured summary of a completed loop.

    Parameters
    ----------
    chat_history : list[dict]
        Messages from the loop (excluding the system message).
    client : OpenRouterClient
    model : str
        Model identifier for the summary call.

    Returns
    -------
    str — the summary text.
    """
    if not chat_history:
        return ""

    transcript = _format_transcript(chat_history)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a concise summariser. Given the following agent conversation "
                "transcript, produce a structured summary covering:\n"
                "1. Objective — what the agent was trying to do\n"
                "2. Actions taken — tools called and key decisions\n"
                "3. Result — final outcome\n"
                "4. Artefacts — any files created/modified or important outputs\n\n"
                "Be concise. Use bullet points."
            ),
        },
        {"role": "user", "content": transcript},
    ]

    try:
        ui.llm_start(model)
        resp = client.chat_completion(
            model=model,
            messages=messages,
            max_tokens=1024,
            temperature=0.2,
        )
        ui.llm_done(resp.get("usage"))
        return resp.get("content") or ""
    except Exception as exc:
        ui.llm_done()
        logger.warning("Summarisation failed: %s", exc)
        return ""


def _safe_summarize(chat_history: list[dict], client: Any, model: str) -> str:
    """Wrapper that never raises."""
    try:
        return summarize_loop(chat_history, client, model)
    except Exception:
        return ""


def _format_transcript(messages: list[dict]) -> str:
    """Flatten a message list into a readable transcript string."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if content:
            lines.append(f"[{role}]: {content[:2000]}")

        # Include tool call info if present
        for tc in msg.get("tool_calls", []):
            func = tc.get("function", {})
            lines.append(
                f"[TOOL_CALL]: {func.get('name', '?')}({func.get('arguments', '')[:500]})"
            )
    return "\n\n".join(lines)
