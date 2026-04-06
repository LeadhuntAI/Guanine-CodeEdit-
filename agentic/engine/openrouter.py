"""
This is your project's agentic engine. Customize as needed.

OpenRouter HTTP client — stdlib-only (urllib).

Provides a thin wrapper around the OpenRouter chat-completion endpoint with
automatic retry, exponential back-off, and normalised response format.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry / back-off defaults
# ---------------------------------------------------------------------------
_MAX_RETRIES = 3
_BACKOFF_SECONDS = (2, 4, 8)


class OpenRouterClient:
    """Lightweight HTTP client for the OpenRouter chat-completion API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat_completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        stop: list[str] | None = None,
    ) -> dict:
        """Send a chat-completion request and return a normalised response.

        Returns
        -------
        dict with keys:
            content   : str | None   — assistant text
            tool_calls: list | None  — tool-call objects (OpenAI format)
            usage     : dict         — token counts
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        if stop:
            payload["stop"] = stop

        raw = self._post("/chat/completions", payload)
        return self._normalise(raw)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        """POST JSON to *path* with retries and exponential back-off."""
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                req = urllib.request.Request(
                    url, data=data, headers=headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body)

            except urllib.error.HTTPError as exc:
                last_exc = exc
                status = exc.code
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                logger.warning(
                    "OpenRouter HTTP %s on attempt %d/%d: %s",
                    status,
                    attempt + 1,
                    _MAX_RETRIES,
                    detail[:300],
                )
                # 429 (rate-limit) or 5xx → retry; anything else → bail
                if status == 429 or status >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_BACKOFF_SECONDS[attempt])
                        continue
                raise OpenRouterError(
                    f"HTTP {status}: {detail[:500]}", status_code=status
                ) from exc

            except urllib.error.URLError as exc:
                last_exc = exc
                logger.warning(
                    "OpenRouter URLError on attempt %d/%d: %s",
                    attempt + 1,
                    _MAX_RETRIES,
                    exc,
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise OpenRouterError(f"URL error: {exc}") from exc

            except json.JSONDecodeError as exc:
                last_exc = exc
                raise OpenRouterError(f"Invalid JSON in response: {exc}") from exc

        # Should not reach here, but be safe.
        raise OpenRouterError(f"All {_MAX_RETRIES} retries exhausted") from last_exc

    @staticmethod
    def _normalise(raw: dict) -> dict:
        """Extract the first choice into a flat dict."""
        choices = raw.get("choices") or []
        if not choices:
            return {"content": None, "tool_calls": None, "usage": raw.get("usage", {})}

        message = choices[0].get("message", {})
        content = message.get("content")
        raw_tool_calls = message.get("tool_calls")

        tool_calls = None
        if raw_tool_calls:
            tool_calls = [
                {
                    "id": tc.get("id", ""),
                    "function": {
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": tc.get("function", {}).get("arguments", ""),
                    },
                }
                for tc in raw_tool_calls
            ]

        return {
            "content": content,
            "tool_calls": tool_calls,
            "usage": raw.get("usage", {}),
        }


class OpenRouterError(Exception):
    """Raised on unrecoverable OpenRouter API errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
