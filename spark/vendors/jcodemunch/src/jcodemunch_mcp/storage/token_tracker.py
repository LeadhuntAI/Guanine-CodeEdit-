"""Persistent token savings tracker.

Records cumulative tokens saved across all tool calls by comparing
raw file sizes against actual MCP response sizes.

Stored in ~/.code-index/_savings.json — a single small JSON file.
No API calls, no file reads — only os.stat for file sizes.

Community meter: token savings are shared anonymously by default to the
global counter at https://j.gravelle.us. Only {"delta": N, "anon_id":
"<uuid>"} is sent — never code, paths, repo names, or anything identifying.
Set JCODEMUNCH_SHARE_SAVINGS=0 to disable.

Performance: uses an in-memory accumulator to avoid disk read+write on every
tool call. Flushes to disk every FLUSH_INTERVAL calls (default 3), on SIGTERM/
SIGINT, and at process exit via atexit. Telemetry batches are sent at flush
time rather than per-call to avoid spawning a new thread on every tool use.
"""

import atexit
import json
import logging
import os
import queue
import signal
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import config as _config

logger = logging.getLogger(__name__)

_SAVINGS_FILE = "_savings.json"
_SESSION_STATS_FILE = "session_stats.json"
_BYTES_PER_TOKEN = 4  # ~4 bytes per token (rough but consistent)
_TELEMETRY_URL = "https://j.gravelle.us/APIs/savings/post.php"
_FLUSH_INTERVAL = 3  # flush to disk every N calls
_RESULT_CACHE_MAXSIZE = 256  # max tool-result cache entries per session

def _get_stats_file_interval() -> int:
    """Read stats_file_interval from config. 0 = disabled, default 3."""
    return max(0, _config.get("stats_file_interval", _FLUSH_INTERVAL))

# Input token pricing ($ per token). Update as models reprice.
# Source: https://claude.com/pricing#api (last verified 2026-03-09)
PRICING = {
    "claude_opus_4_6":    5.00 / 1_000_000,  # Claude Opus 4.6   — $5.00 / 1M input tokens (≤200K ctx)
    "claude_sonnet_4_6":  3.00 / 1_000_000,  # Claude Sonnet 4.6 — $3.00 / 1M input tokens (≤200K ctx)
    "claude_haiku_4_5":   1.00 / 1_000_000,  # Claude Haiku 4.5  — $1.00 / 1M input tokens
    "gpt5_latest":       10.00 / 1_000_000,  # GPT-5 (latest)    — $10.00 / 1M input tokens
}


# ---------------------------------------------------------------------------
# In-memory state (process-lifetime cache)
# ---------------------------------------------------------------------------

class _State:
    """Holds the in-memory accumulator for the current process."""
    def __init__(self):
        self._lock = threading.Lock()
        self._loaded = False
        self._total: int = 0          # cumulative total (disk + in-flight)
        self._unflushed: int = 0      # delta not yet written to disk
        self._call_count: int = 0     # calls since last savings flush
        self._stats_call_count: int = 0  # calls since last session_stats.json write
        self._anon_id: Optional[str] = None
        self._base_path: Optional[str] = None
        self._pending_telemetry: int = 0  # unflushed delta for telemetry
        # Session-level tracking (process lifetime only, not persisted)
        self._session_tokens: int = 0
        self._session_calls: int = 0
        self._session_start: float = time.monotonic()
        self._session_tool_breakdown: dict = {}
        # Session-level tool-result cache (LRU, evicted at _RESULT_CACHE_MAXSIZE)
        self._result_cache: OrderedDict = OrderedDict()  # (tool, repo, key) -> result
        self._cache_hits: dict = {}    # tool_name -> hit count
        self._cache_misses: dict = {}  # tool_name -> miss count

    def _ensure_loaded(self, base_path: Optional[str]) -> None:
        """Load persisted total from disk (once per process)."""
        if self._loaded:
            return
        self._base_path = base_path
        path = _savings_path(base_path)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except Exception:
            logger.debug("Failed to load savings data from %s", path, exc_info=True)
            data = {}
        self._total = data.get("total_tokens_saved", 0)
        self._anon_id = data.get("anon_id")
        self._loaded = True

    def add(self, delta: int, base_path: Optional[str], tool_name: Optional[str] = None) -> int:
        """Add delta to the running total. Returns new cumulative total."""
        with self._lock:
            self._ensure_loaded(base_path)
            delta = max(0, delta)
            self._total += delta
            self._unflushed += delta
            self._pending_telemetry += delta
            self._session_tokens += delta
            self._session_calls += 1
            if tool_name:
                self._session_tool_breakdown[tool_name] = (
                    self._session_tool_breakdown.get(tool_name, 0) + delta
                )
            self._call_count += 1
            if self._call_count >= _FLUSH_INTERVAL:
                self._flush_locked()
            return self._total

    def session_stats(self, base_path: Optional[str]) -> dict:
        """Return session-level stats (process lifetime)."""
        with self._lock:
            self._ensure_loaded(base_path)
            stats = self._build_stats_locked()
            self._write_session_stats_locked(stats, force=True)
            return stats

    def cache_get(self, tool_name: str, repo: str, specific_key: tuple):
        """Return cached result for (tool, repo, key), or None on miss. Thread-safe."""
        with self._lock:
            full_key = (tool_name, repo, specific_key)
            if full_key in self._result_cache:
                self._result_cache.move_to_end(full_key)
                self._cache_hits[tool_name] = self._cache_hits.get(tool_name, 0) + 1
                return self._result_cache[full_key]
            self._cache_misses[tool_name] = self._cache_misses.get(tool_name, 0) + 1
            return None

    def cache_put(self, tool_name: str, repo: str, specific_key: tuple, result: dict) -> None:
        """Store result in LRU cache. Evicts oldest entry when full. Thread-safe."""
        with self._lock:
            full_key = (tool_name, repo, specific_key)
            self._result_cache[full_key] = result
            self._result_cache.move_to_end(full_key)
            if len(self._result_cache) > _RESULT_CACHE_MAXSIZE:
                self._result_cache.popitem(last=False)

    def cache_invalidate(self, repo: Optional[str] = None) -> int:
        """Evict all entries (repo=None) or entries for a specific repo. Returns evicted count."""
        with self._lock:
            if repo is None:
                count = len(self._result_cache)
                self._result_cache.clear()
                return count
            to_delete = [k for k in self._result_cache if k[1] == repo]
            for k in to_delete:
                del self._result_cache[k]
            return len(to_delete)

    def cache_stats(self) -> dict:
        """Return cache hit/miss stats. Thread-safe."""
        with self._lock:
            total_hits = sum(self._cache_hits.values())
            total_misses = sum(self._cache_misses.values())
            total_lookups = total_hits + total_misses
            by_tool = {}
            all_tools = set(self._cache_hits) | set(self._cache_misses)
            for tool in all_tools:
                h = self._cache_hits.get(tool, 0)
                m = self._cache_misses.get(tool, 0)
                t = h + m
                by_tool[tool] = {
                    "hits": h,
                    "misses": m,
                    "hit_rate": round(h / t, 3) if t else 0.0,
                }
            return {
                "total_hits": total_hits,
                "total_misses": total_misses,
                "hit_rate": round(total_hits / total_lookups, 3) if total_lookups else 0.0,
                "cached_entries": len(self._result_cache),
                "by_tool": by_tool,
            }

    def _build_stats_locked(self) -> dict:
        """Build session stats dict. Must be called with _lock held."""
        elapsed = time.monotonic() - self._session_start
        # Build cache stats inline (re-uses lock already held)
        total_hits = sum(self._cache_hits.values())
        total_misses = sum(self._cache_misses.values())
        total_lookups = total_hits + total_misses
        cache_stats = {
            "total_hits": total_hits,
            "total_misses": total_misses,
            "hit_rate": round(total_hits / total_lookups, 3) if total_lookups else 0.0,
            "cached_entries": len(self._result_cache),
        }
        return {
            "session_tokens_saved": self._session_tokens,
            "session_calls": self._session_calls,
            "session_duration_s": round(elapsed, 1),
            "total_tokens_saved": self._total,
            "tool_breakdown": dict(self._session_tool_breakdown),
            "result_cache": cache_stats,
        }

    def _write_session_stats_locked(self, stats: dict, force: bool = False) -> None:
        """Write session stats to ~/.code-index/session_stats.json. Must be called with _lock held.

        Writes are gated by stats_file_interval config (default 3 calls).
        Set to 0 to disable all session_stats.json writes.
        Pass force=True to bypass the interval check (e.g. explicit get_session_stats call).
        """
        stats_file_interval = _get_stats_file_interval()
        if stats_file_interval == 0 and not force:
            return
        self._stats_call_count += 1
        if not force and self._stats_call_count < stats_file_interval:
            return
        self._stats_call_count = 0
        path = _session_stats_path(self._base_path)
        try:
            payload = {**stats, "last_updated": datetime.now(timezone.utc).isoformat()}
            path.write_text(json.dumps(payload, indent=2))
        except Exception:
            logger.debug("Failed to write session stats to %s", path, exc_info=True)

    def get_total(self, base_path: Optional[str]) -> int:
        with self._lock:
            self._ensure_loaded(base_path)
            return self._total

    def _flush_locked(self) -> None:
        """Write accumulated total to disk. Must be called with _lock held."""
        if self._unflushed == 0 and self._loaded:
            self._call_count = 0
            self._write_session_stats_locked(self._build_stats_locked())
            return
        path = _savings_path(self._base_path)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except Exception:
            logger.debug("Failed to read savings file for flush: %s", path, exc_info=True)
            data = {}
        if self._anon_id is None:
            if "anon_id" not in data:
                data["anon_id"] = str(uuid.uuid4())
            self._anon_id = data["anon_id"]
        else:
            data["anon_id"] = self._anon_id
        data["total_tokens_saved"] = data.get("total_tokens_saved", 0) + self._unflushed
        try:
            path.write_text(json.dumps(data))
        except Exception:
            logger.debug("Failed to write savings data to %s", path, exc_info=True)

        # Send batched telemetry
        if self._pending_telemetry > 0 and _config.get("share_savings", True):
            _share_savings(self._pending_telemetry, self._anon_id)
            self._pending_telemetry = 0

        self._unflushed = 0
        self._call_count = 0
        self._write_session_stats_locked(self._build_stats_locked())

    def flush(self) -> None:
        """Public flush — called at atexit."""
        with self._lock:
            if self._loaded:
                self._flush_locked()


_state = _State()
atexit.register(_state.flush)


def _signal_flush(signum, frame):
    """Flush savings to disk on SIGTERM/SIGINT, then re-raise the signal."""
    _state.flush()
    # Restore the default handler and re-raise so the process exits normally.
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


# MCP servers are commonly killed via SIGTERM (pipe close, client shutdown).
# atexit does NOT run on SIGTERM, so we register explicit handlers here.
# We only install if no handler is already set (respects user overrides).
for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        if signal.getsignal(_sig) in (signal.SIG_DFL, None):
            signal.signal(_sig, _signal_flush)
    except (OSError, ValueError):
        # Signals can't be set in non-main threads; ignore safely.
        pass


# ---------------------------------------------------------------------------
# Public API (unchanged signatures)
# ---------------------------------------------------------------------------

def _savings_path(base_path: Optional[str] = None) -> Path:
    root = Path(base_path) if base_path else Path.home() / ".code-index"
    root.mkdir(parents=True, exist_ok=True)
    return root / _SAVINGS_FILE


def _session_stats_path(base_path: Optional[str] = None) -> Path:
    root = Path(base_path) if base_path else Path.home() / ".code-index"
    root.mkdir(parents=True, exist_ok=True)
    return root / _SESSION_STATS_FILE


# ---------------------------------------------------------------------------
# Telemetry worker (P11)
# ---------------------------------------------------------------------------
# A single long-lived daemon thread drains _telemetry_queue instead of
# spawning a new thread on every flush.  This eliminates per-flush thread
# creation overhead and prevents thread pile-up under rapid calls.
# ---------------------------------------------------------------------------

_telemetry_queue: queue.Queue = queue.Queue()


def _telemetry_worker() -> None:
    """Drain _telemetry_queue and POST each item. Runs for process lifetime."""
    while True:
        item = _telemetry_queue.get()
        if item is None:  # shutdown sentinel
            break
        delta, anon_id = item
        try:
            import httpx
            httpx.post(
                _TELEMETRY_URL,
                json={"delta": delta, "anon_id": anon_id},
                timeout=3.0,
            )
        except Exception:
            logger.debug("Telemetry post failed", exc_info=True)
        finally:
            _telemetry_queue.task_done()


threading.Thread(
    target=_telemetry_worker, daemon=True, name="jcodemunch-telemetry"
).start()


def _share_savings(delta: int, anon_id: str) -> None:
    """Enqueue a fire-and-forget POST to the community meter. Never raises."""
    _telemetry_queue.put((delta, anon_id))


def record_savings(tokens_saved: int, base_path: Optional[str] = None, tool_name: Optional[str] = None) -> int:
    """Add tokens_saved to the running total. Returns new cumulative total.

    Uses an in-memory accumulator; flushes to disk every FLUSH_INTERVAL calls (currently 3) and at exit.
    """
    return _state.add(tokens_saved, base_path, tool_name)


def get_session_stats(base_path: Optional[str] = None) -> dict:
    """Return token savings stats for the current session (process lifetime).

    Returns session_tokens_saved, session_calls, session_duration_s,
    total_tokens_saved (all-time), tool_breakdown, and cost_avoided estimates.
    """
    stats = _state.session_stats(base_path)
    session_tokens = stats["session_tokens_saved"]
    total_tokens = stats["total_tokens_saved"]
    return {
        **stats,
        "session_cost_avoided": {
            model: round(session_tokens * rate, 4)
            for model, rate in PRICING.items()
        },
        "total_cost_avoided": {
            model: round(total_tokens * rate, 4)
            for model, rate in PRICING.items()
        },
    }


def get_total_saved(base_path: Optional[str] = None) -> int:
    """Return the current cumulative total without modifying it."""
    return _state.get_total(base_path)


def result_cache_get(tool_name: str, repo: str, specific_key: tuple):
    """Return a cached tool result, or None on miss. Updates hit/miss counters."""
    return _state.cache_get(tool_name, repo, specific_key)


def result_cache_put(tool_name: str, repo: str, specific_key: tuple, result: dict) -> None:
    """Store a tool result in the session LRU cache (max 256 entries)."""
    _state.cache_put(tool_name, repo, specific_key, result)


def result_cache_invalidate(repo: Optional[str] = None) -> int:
    """Evict cached results — all repos (default) or a specific repo. Returns evicted count."""
    return _state.cache_invalidate(repo)


def result_cache_stats() -> dict:
    """Return cache hit/miss stats for the current session."""
    return _state.cache_stats()


def estimate_savings(raw_bytes: int, response_bytes: int) -> int:
    """Estimate tokens saved: (raw - response) / bytes_per_token."""
    return max(0, (raw_bytes - response_bytes) // _BYTES_PER_TOKEN)


def cost_avoided(tokens_saved: int, total_tokens_saved: int) -> dict:
    """Formerly returned per-call cost breakdowns for _meta envelopes.

    Now returns an empty dict — cost detail is available via get_session_stats
    only. Removing 4-model cost tables from every per-tool _meta response
    reduces conversation-history token overhead by ~70 tokens/call.
    """
    return {}
