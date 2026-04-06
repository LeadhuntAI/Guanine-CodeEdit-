"""
Spark debug mode — logs full API requests and responses.

When enabled, patches OpenRouterClient to log every request/response
to both stderr (abbreviated) and a debug log file (full payloads).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any


_debug_file = None
_debug_enabled = False
_call_counter = 0


def enable_debug(target_dir: str) -> None:
    """Enable debug mode: patch the OpenRouter client and open log file."""
    global _debug_file, _debug_enabled

    # Create debug log file
    log_dir = os.path.join(target_dir, ".claude")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"spark_debug_{timestamp}.log")

    _debug_file = open(log_path, "w", encoding="utf-8")
    _debug_enabled = True

    _log(f"Spark debug log started at {datetime.now(timezone.utc).isoformat()}")
    _log(f"Target directory: {target_dir}")

    # Patch the OpenRouter client
    _patch_openrouter()

    _stderr(f"  [DEBUG] Logging to {log_path}")


def _stderr(msg: str) -> None:
    """Write to stderr in dim grey (debug output should recede visually)."""
    from spark.ui import ui
    c = ui.c
    sys.stderr.write(f"{c.GREY}{msg}{c.RESET}\n")
    sys.stderr.flush()


def _log(msg: str) -> None:
    """Write to the debug log file."""
    if _debug_file:
        _debug_file.write(msg + "\n")
        _debug_file.flush()


def _fmt_messages(messages: list[dict]) -> str:
    """Format messages for terminal display (abbreviated)."""
    lines = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")

        if role == "system":
            lines.append(f"    SYSTEM: ({len(content or '')} chars)")
        elif role == "user":
            preview = (content or "")[:200]
            if len(content or "") > 200:
                preview += "..."
            lines.append(f"    USER: {preview}")
        elif role == "assistant":
            if tool_calls:
                names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                lines.append(f"    ASSISTANT: [tool_calls: {', '.join(names)}]")
            elif content:
                preview = content[:200]
                if len(content) > 200:
                    preview += "..."
                lines.append(f"    ASSISTANT: {preview}")
        elif role == "tool":
            tc_id = msg.get("tool_call_id", "?")
            preview = (content or "")[:100]
            if len(content or "") > 100:
                preview += "..."
            lines.append(f"    TOOL [{tc_id[:12]}]: {preview}")
    return "\n".join(lines)


def _fmt_response(resp: dict) -> str:
    """Format a normalised response for terminal display."""
    lines = []
    content = resp.get("content")
    tool_calls = resp.get("tool_calls")
    usage = resp.get("usage", {})

    if content:
        preview = content[:300]
        if len(content) > 300:
            preview += f"... ({len(content)} chars total)"
        lines.append(f"    Content: {preview}")

    if tool_calls:
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "?")
            args = func.get("arguments", "")
            args_preview = args[:150]
            if len(args) > 150:
                args_preview += "..."
            lines.append(f"    Tool call: {name}({args_preview})")

    if usage:
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        total = usage.get("total_tokens", prompt + completion)
        lines.append(f"    Tokens: {total} ({prompt} in / {completion} out)")

    return "\n".join(lines)


def _patch_openrouter() -> None:
    """Monkey-patch OpenRouterClient.chat_completion to log requests/responses."""
    from spark.engine.openrouter import OpenRouterClient

    original_chat = OpenRouterClient.chat_completion

    def debug_chat_completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        stop: list[str] | None = None,
    ) -> dict:
        global _call_counter
        _call_counter += 1
        call_id = _call_counter

        # --- Log request ---
        _stderr(f"\n  [DEBUG] ━━━ API Call #{call_id} ━━━")
        _stderr(f"  [DEBUG] Model: {model}")
        _stderr(f"  [DEBUG] Messages ({len(messages)}):")
        _stderr(_fmt_messages(messages))
        if tools:
            tool_names = [t.get("function", {}).get("name", "?") for t in tools]
            _stderr(f"  [DEBUG] Tools: {', '.join(tool_names)}")
        _stderr(f"  [DEBUG] max_tokens={max_tokens} temperature={temperature}")

        # Full payload to log file
        _log(f"\n{'=' * 80}")
        _log(f"API Call #{call_id} at {datetime.now(timezone.utc).isoformat()}")
        _log(f"Model: {model}")
        _log(f"max_tokens={max_tokens} temperature={temperature}")
        _log(f"\n--- REQUEST MESSAGES ---")
        _log(json.dumps(messages, indent=2, ensure_ascii=False)[:50000])
        if tools:
            _log(f"\n--- TOOLS ---")
            _log(json.dumps(tools, indent=2, ensure_ascii=False)[:10000])

        # --- Make the call ---
        start = time.monotonic()
        try:
            resp = original_chat(
                self, model=model, messages=messages, tools=tools,
                max_tokens=max_tokens, temperature=temperature, stop=stop,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            _stderr(f"  [DEBUG] ✗ FAILED after {elapsed:.1f}s: {exc}")
            _log(f"\n--- ERROR ({elapsed:.1f}s) ---")
            _log(str(exc))
            raise

        elapsed = time.monotonic() - start

        # --- Log response ---
        _stderr(f"  [DEBUG] ✓ Response ({elapsed:.1f}s):")
        _stderr(_fmt_response(resp))

        _log(f"\n--- RESPONSE ({elapsed:.1f}s) ---")
        # Log full response but cap content at 20k chars
        log_resp = dict(resp)
        if log_resp.get("content") and len(log_resp["content"]) > 20000:
            log_resp["content"] = log_resp["content"][:20000] + "\n... (truncated)"
        _log(json.dumps(log_resp, indent=2, ensure_ascii=False))
        _log(f"{'=' * 80}")

        return resp

    OpenRouterClient.chat_completion = debug_chat_completion
