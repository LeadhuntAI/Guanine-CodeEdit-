"""
This is your project's agentic engine. Customize as needed.

Workflow runner — session state, layer dispatch, Jinja2 template processing.

Orchestrates multi-layer workflows where each layer can be a simple prompt,
an agentic loop, a template render, or a direct tool call.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

import jinja2

from .openrouter import OpenRouterClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class LightweightWorkflowSession:
    """Accumulated state for a multi-layer workflow execution.

    Attributes
    ----------
    context : dict
        Shared context dict — updated after each layer completes.
    chat_history : list[dict]
        Full conversation history across all layers.
    outputs : list[dict]
        Per-layer output records.
    current_layer : int
        Zero-based index of the layer currently executing.
    """

    def __init__(self) -> None:
        self.context: dict[str, Any] = {}
        self.chat_history: list[dict] = []
        self.outputs: list[dict] = []
        self.current_layer: int = 0

    # --- mutation helpers ---

    def add_input(self, obj: dict) -> None:
        """Merge *obj* into the session context."""
        if isinstance(obj, dict):
            self.context.update(obj)

    def get_input(self) -> dict:
        """Return a shallow copy of the current context."""
        return dict(self.context)

    def log_output(self, **kwargs: Any) -> None:
        """Record the output of the current layer."""
        record = {"layer": self.current_layer, **kwargs}
        self.outputs.append(record)

    def get_current_layer(self) -> int:
        """Return the zero-based current layer index."""
        return self.current_layer

    def to_dict(self) -> dict:
        """Serialise the full session state."""
        return {
            "context": copy.deepcopy(self.context),
            "chat_history": list(self.chat_history),
            "outputs": list(self.outputs),
            "current_layer": self.current_layer,
        }


# ---------------------------------------------------------------------------
# Placeholder / Jinja2 template processing
# ---------------------------------------------------------------------------

def _resolve_placeholders(text: str, session: LightweightWorkflowSession) -> str:
    """Replace legacy ``{PLO}`` and ``{OutputLayer-N}`` tokens.

    * ``{PLO}``          → the ``response`` field from the previous layer
    * ``{OutputLayer-N}``→ the ``response`` field from layer N (0-based)
    """
    if not text:
        return text

    # {PLO} — previous layer output
    if "{PLO}" in text:
        prev = ""
        if session.outputs:
            prev = session.outputs[-1].get("response", "")
        text = text.replace("{PLO}", str(prev))

    # {OutputLayer-N}
    def _output_repl(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(session.outputs):
            return str(session.outputs[idx].get("response", ""))
        return m.group(0)  # leave unresolved

    text = re.sub(r"\{OutputLayer-(\d+)\}", _output_repl, text)
    return text


def _render_jinja(template_str: str, context: dict) -> str:
    """Render *template_str* through Jinja2 with *context* variables.

    Uses ``jinja2.BaseLoader`` so no filesystem access is needed.
    Undefined variables render as empty strings (``Undefined``).
    """
    try:
        env = jinja2.Environment(
            loader=jinja2.BaseLoader(),
            undefined=jinja2.Undefined,
        )
        tmpl = env.from_string(template_str)
        return tmpl.render(**context)
    except jinja2.TemplateError as exc:
        logger.warning("Jinja2 render error: %s", exc)
        return template_str  # return unprocessed on error


# ---------------------------------------------------------------------------
# Layer processors
# ---------------------------------------------------------------------------

def _process_prompt(
    layer_def: dict,
    session: LightweightWorkflowSession,
    client: OpenRouterClient,
) -> dict:
    """Process a simple prompt layer: template → LLM call → response."""
    prompt = layer_def.get("prompt", "")

    # Resolve legacy placeholders first, then Jinja2
    prompt = _resolve_placeholders(prompt, session)
    prompt = _render_jinja(prompt, session.context)

    system_msg = layer_def.get("system_message", "")
    if system_msg:
        system_msg = _resolve_placeholders(system_msg, session)
        system_msg = _render_jinja(system_msg, session.context)

    messages: list[dict] = []
    if system_msg:
        messages.append({"role": "system", "content": system_msg})
    messages.append({"role": "user", "content": prompt})

    model = layer_def.get("model", "openai/gpt-4o-mini")

    try:
        resp = client.chat_completion(
            model=model,
            messages=messages,
            max_tokens=layer_def.get("max_tokens", 4096),
            temperature=layer_def.get("temperature", 0.3),
        )
        content = resp.get("content") or ""
    except Exception as exc:
        logger.error("Prompt layer LLM call failed: %s", exc)
        content = f"Error: {exc}"

    return {"response": content}


def _process_smart_template(
    layer_def: dict,
    session: LightweightWorkflowSession,
) -> dict:
    """Render a Jinja2 template with session context — no AI call."""
    template = layer_def.get("template", layer_def.get("prompt", ""))
    template = _resolve_placeholders(template, session)
    rendered = _render_jinja(template, session.context)
    return {"response": rendered}


def _process_tool(
    layer_def: dict,
    session: LightweightWorkflowSession,
    tool_registry: dict,
) -> dict:
    """Execute a single tool directly."""
    from .tool_executor import execute_tool_call

    tool_name = layer_def.get("tool_name", "")
    tool_args = layer_def.get("tool_args", {})

    if isinstance(tool_args, str):
        tool_args = _resolve_placeholders(tool_args, session)
        tool_args = _render_jinja(tool_args, session.context)

    result = execute_tool_call(tool_registry, tool_name, tool_args)
    return {"response": result}


# ---------------------------------------------------------------------------
# Layer dispatch
# ---------------------------------------------------------------------------

def process_layer(
    layer_def: dict,
    session: LightweightWorkflowSession,
    client: OpenRouterClient,
    tool_registry: dict | None = None,
) -> dict:
    """Dispatch a single workflow layer based on its ``layer_type``.

    Supported types: ``prompt``, ``agentic_loop``, ``smart_template``, ``tool``.

    Returns a dict with at least a ``response`` key.
    """
    tool_registry = tool_registry or {}
    layer_type = layer_def.get("layer_type", "prompt")

    try:
        if layer_type == "prompt":
            return _process_prompt(layer_def, session, client)

        elif layer_type == "agentic_loop":
            from .loop import process_agentic_loop, process_agentic_loop_native
            from .knowledge import resolve_knowledge

            # Resolve knowledge if the layer specifies it
            knowledge_cfg = layer_def.get("knowledge", {})
            knowledge = resolve_knowledge(
                rules=knowledge_cfg.get("rules", []),
                skills=knowledge_cfg.get("skills", []),
                knowledge_set=knowledge_cfg.get("knowledge_set", []),
                base_dir=knowledge_cfg.get("base_dir", "."),
            )

            mode = layer_def.get("loop_mode", "react")
            if mode == "native":
                return process_agentic_loop_native(
                    layer_def, session, client, tool_registry, knowledge
                )
            else:
                return process_agentic_loop(
                    layer_def, session, client, tool_registry, knowledge
                )

        elif layer_type == "smart_template":
            return _process_smart_template(layer_def, session)

        elif layer_type == "tool":
            return _process_tool(layer_def, session, tool_registry)

        else:
            logger.warning("Unknown layer_type %r — treating as prompt", layer_type)
            return _process_prompt(layer_def, session, client)

    except Exception as exc:
        logger.exception("Layer processing failed: %s", exc)
        return {"response": f"Error: {exc}"}


# ---------------------------------------------------------------------------
# Full workflow runner
# ---------------------------------------------------------------------------

def run_workflow(
    workflow_json: dict,
    client: OpenRouterClient,
    tool_registry: dict | None = None,
    input_data: dict | None = None,
) -> dict:
    """Execute a complete multi-layer workflow.

    Parameters
    ----------
    workflow_json : dict
        Workflow definition with a ``layers`` key (list of layer defs).
    client : OpenRouterClient
    tool_registry : dict | None
        ``{"tool_name": callable, ...}``
    input_data : dict | None
        Initial context data seeded into the session.

    Returns
    -------
    dict — the full session state via ``session.to_dict()``.
    """
    tool_registry = tool_registry or {}
    session = LightweightWorkflowSession()

    if input_data:
        session.add_input(input_data)

    layers = workflow_json.get("layers", [])

    for i, layer_def in enumerate(layers):
        session.current_layer = i
        logger.info(
            "Processing layer %d/%d: %s",
            i + 1,
            len(layers),
            layer_def.get("name", layer_def.get("layer_type", "unknown")),
        )

        result = process_layer(layer_def, session, client, tool_registry)

        # Store the output
        session.log_output(**result)

        # Update context with the layer's response
        response = result.get("response", "")
        session.context[f"layer_{i}_output"] = response
        session.context["last_output"] = response

        # Merge any extra context the layer produced
        if "context_updates" in result:
            session.add_input(result["context_updates"])

        # Append to chat history if relevant
        if "chat_history" in result:
            session.chat_history.extend(result["chat_history"])

    return session.to_dict()
