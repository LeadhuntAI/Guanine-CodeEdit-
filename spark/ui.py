"""
Spark terminal UI — progress feedback, spinners, phase tracking.

Stdlib-only. Provides a singleton ``ui`` that all Spark modules call to
show progress. Thread-safe for concurrent explorer/doc-writer phases.
Supports ANSI colors on TTYs for visual hierarchy.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import Optional


# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    """Check if the terminal supports ANSI colours."""
    if not hasattr(sys.stderr, "isatty") or not sys.stderr.isatty():
        return False
    # Windows: enable VT processing
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # STD_ERROR_HANDLE = -12
            handle = kernel32.GetStdHandle(-12)
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass
    return True


class _Colors:
    """ANSI escape codes. All empty strings when colour is disabled."""

    def __init__(self, enabled: bool) -> None:
        if enabled:
            self.RESET = "\033[0m"
            self.BOLD = "\033[1m"
            self.DIM = "\033[2m"           # grey / dim
            self.ITALIC = "\033[3m"
            # Foreground
            self.CYAN = "\033[36m"
            self.GREEN = "\033[32m"
            self.YELLOW = "\033[33m"
            self.RED = "\033[31m"
            self.BLUE = "\033[34m"
            self.MAGENTA = "\033[35m"
            self.WHITE = "\033[97m"
            self.GREY = "\033[90m"         # bright-black = grey
            # Combos
            self.PHASE = "\033[1;36m"      # bold cyan
            self.SUCCESS = "\033[1;32m"    # bold green
            self.ERROR = "\033[1;31m"      # bold red
            self.WARN = "\033[1;33m"       # bold yellow
            self.HEADER = "\033[1;97m"     # bold white
            self.ACCENT = "\033[35m"       # magenta
            self.SEPARATOR = "\033[90m"    # grey
            self.STAT_LABEL = "\033[36m"   # cyan
            self.STAT_VALUE = "\033[1;97m" # bold white
        else:
            for attr in (
                "RESET", "BOLD", "DIM", "ITALIC",
                "CYAN", "GREEN", "YELLOW", "RED", "BLUE", "MAGENTA", "WHITE", "GREY",
                "PHASE", "SUCCESS", "ERROR", "WARN", "HEADER", "ACCENT",
                "SEPARATOR", "STAT_LABEL", "STAT_VALUE",
            ):
                setattr(self, attr, "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_elapsed(seconds: float) -> str:
    """Format seconds as '1m 23s' or '45s'."""
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _bar(fraction: float, width: int = 20) -> str:
    """Render a progress bar: [████░░░░░░]"""
    filled = int(fraction * width)
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}]"


# ---------------------------------------------------------------------------
# SparkUI
# ---------------------------------------------------------------------------

class SparkUI:
    """Thread-safe terminal feedback for Spark runs."""

    _SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _SPINNER_INTERVAL = 0.1  # seconds
    _STATUS_INTERVAL = 30.0  # seconds between periodic status lines

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spinner_event = threading.Event()
        self._spinner_thread: Optional[threading.Thread] = None
        self._spinner_msg = ""
        self._is_tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()

        # Colours
        self.c = _Colors(_supports_color())

        # Phase tracking
        self._phase_times: dict[str, float] = {}
        self._current_phase: Optional[str] = None
        self._current_phase_detail: str = ""

        # Concurrent worker tracking
        self._workers: dict[str, str] = {}  # id -> "active" | "done" | "failed"

        # Periodic status
        self._status_thread: Optional[threading.Thread] = None
        self._status_event = threading.Event()
        self._last_status_time = 0.0

        # Stats
        self._stats = {
            "total_start": 0.0,
            "llm_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "tool_calls": 0,
            "phases": [],
            "errors": [],
        }

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _write(self, msg: str, end: str = "\n") -> None:
        """Write a line to stderr, clearing any active spinner first."""
        with self._lock:
            if self._spinner_event.is_set() and self._is_tty:
                # Clear spinner line
                sys.stderr.write("\r" + " " * 80 + "\r")
            sys.stderr.write(msg + end)
            sys.stderr.flush()

    def debug(self, msg: str) -> None:
        """Write a debug-level message in dim grey."""
        c = self.c
        self._write(f"{c.GREY}  {msg}{c.RESET}")

    def info(self, msg: str) -> None:
        """Write an info-level message."""
        self._write(f"  {msg}")

    def warn(self, msg: str) -> None:
        """Write a warning message in yellow."""
        c = self.c
        self._write(f"{c.WARN}  ⚠ {msg}{c.RESET}")

    def error(self, msg: str) -> None:
        """Write an error message in red."""
        c = self.c
        self._write(f"{c.ERROR}  ✗ {msg}{c.RESET}")

    # ------------------------------------------------------------------
    # Phase management
    # ------------------------------------------------------------------

    def phase(self, name: str, detail: str = "") -> None:
        """Start a new phase, closing the previous one if active."""
        if self._current_phase:
            self.phase_end()

        self._current_phase = name
        self._current_phase_detail = detail
        self._phase_times[name] = time.monotonic()

        c = self.c
        sep = f"{c.SEPARATOR}{'─' * 60}{c.RESET}"
        label = f"  {c.PHASE}▶ {name}{c.RESET}"
        if detail:
            label += f" {c.DIM}— {detail}{c.RESET}"

        self._write(f"\n{sep}")
        self._write(label)
        self._write(sep)

    def phase_end(self, summary: str = "") -> None:
        """End the current phase, printing elapsed time."""
        name = self._current_phase
        if not name:
            return

        elapsed = time.monotonic() - self._phase_times.get(name, time.monotonic())
        self._stats["phases"].append({"name": name, "elapsed": elapsed})

        c = self.c
        line = f"  {c.SUCCESS}✓ {name}{c.RESET} {c.DIM}done ({_fmt_elapsed(elapsed)}){c.RESET}"
        if summary:
            line += f" {c.DIM}— {summary}{c.RESET}"
        self._write(line)
        self._current_phase = None
        self._current_phase_detail = ""

    # ------------------------------------------------------------------
    # Spinner
    # ------------------------------------------------------------------

    def spinner_start(self, message: str) -> None:
        """Show an animated spinner with *message*. Noop if not a TTY."""
        self._spinner_msg = message
        if not self._is_tty:
            self._write(f"  {message}")
            return

        self._spinner_event.set()
        if self._spinner_thread is None or not self._spinner_thread.is_alive():
            self._spinner_thread = threading.Thread(
                target=self._spinner_loop, daemon=True
            )
            self._spinner_thread.start()

    def spinner_stop(self, result: str = "") -> None:
        """Stop the spinner, optionally printing a result line."""
        if not self._spinner_event.is_set():
            return
        self._spinner_event.clear()
        if self._spinner_thread and self._spinner_thread.is_alive():
            self._spinner_thread.join(timeout=0.5)
        self._spinner_thread = None

        if self._is_tty:
            with self._lock:
                sys.stderr.write("\r" + " " * 80 + "\r")
                sys.stderr.flush()

        if result:
            self._write(f"  {result}")

    def _spinner_loop(self) -> None:
        """Background thread: animate the spinner."""
        c = self.c
        idx = 0
        frames = self._SPINNER_FRAMES
        while self._spinner_event.is_set():
            frame = frames[idx % len(frames)]
            with self._lock:
                sys.stderr.write(f"\r  {c.CYAN}{frame}{c.RESET} {c.DIM}{self._spinner_msg}{c.RESET}")
                sys.stderr.flush()
            idx += 1
            time.sleep(self._SPINNER_INTERVAL)

    @contextmanager
    def spin(self, message: str, done_msg: str = ""):
        """Context manager for a spinner."""
        self.spinner_start(message)
        try:
            yield
        finally:
            self.spinner_stop(done_msg)

    # ------------------------------------------------------------------
    # Concurrent worker tracking
    # ------------------------------------------------------------------

    def track_start(self, worker_id: str) -> None:
        """Register a concurrent worker as active."""
        with self._lock:
            self._workers[worker_id] = "active"
        active = sum(1 for v in self._workers.values() if v == "active")
        done = sum(1 for v in self._workers.values() if v == "done")
        total = len(self._workers)

        c = self.c
        self._write(
            f"  {c.BLUE}▸{c.RESET} {worker_id} "
            f"{c.DIM}({done}/{total} done, {active} active){c.RESET}"
        )

    def track_done(self, worker_id: str, detail: str = "") -> None:
        """Mark a worker as completed."""
        with self._lock:
            self._workers[worker_id] = "done"
        done = sum(1 for v in self._workers.values() if v == "done")
        total = len(self._workers)
        fraction = done / total if total else 0

        c = self.c
        msg = f"  {c.GREEN}✓{c.RESET} {worker_id}"
        if detail:
            msg += f" {c.DIM}— {detail}{c.RESET}"
        msg += f" {c.DIM}{_bar(fraction)} {done}/{total}{c.RESET}"
        self._write(msg)

    def track_fail(self, worker_id: str, reason: str = "") -> None:
        """Mark a worker as failed."""
        with self._lock:
            self._workers[worker_id] = "failed"

        c = self.c
        msg = f"  {c.ERROR}✗ {worker_id}{c.RESET}"
        if reason:
            msg += f" {c.DIM}— {reason}{c.RESET}"
        self._write(msg)
        self._stats["errors"].append(f"{worker_id}: {reason}")

    def track_reset(self) -> None:
        """Clear worker tracking for a new concurrent phase."""
        with self._lock:
            self._workers.clear()

    # ------------------------------------------------------------------
    # LLM / tool call tracking
    # ------------------------------------------------------------------

    def llm_start(self, model: str) -> None:
        """Record start of an LLM call, show spinner."""
        short_model = model.rsplit("/", 1)[-1] if "/" in model else model
        self.spinner_start(f"Calling {short_model}...")

    def llm_done(self, usage: dict | None = None) -> None:
        """Record completion of an LLM call."""
        self.spinner_stop()
        self._stats["llm_calls"] += 1
        if usage:
            self._stats["tokens_in"] += usage.get("prompt_tokens", 0)
            self._stats["tokens_out"] += usage.get("completion_tokens", 0)

    def tool_call(self, name: str, brief_args: str = "") -> None:
        """Show a tool call notification."""
        self._stats["tool_calls"] += 1
        c = self.c
        msg = f"    {c.GREY}↳ {name}"
        if brief_args:
            msg += f": {brief_args[:60]}"
        msg += c.RESET
        self._write(msg)

    def iteration(self, current: int, total: int, label: str = "") -> None:
        """Show iteration progress."""
        c = self.c
        msg = f"  {c.ACCENT}[{current}/{total}]{c.RESET}"
        if label:
            msg += f" {label}"
        self._write(msg)

    # ------------------------------------------------------------------
    # Periodic status line
    # ------------------------------------------------------------------

    def _start_status_ticker(self) -> None:
        """Start a background thread that prints a status line periodically."""
        if not self._is_tty:
            return
        self._status_event.set()
        self._last_status_time = time.monotonic()
        self._status_thread = threading.Thread(target=self._status_loop, daemon=True)
        self._status_thread.start()

    def _stop_status_ticker(self) -> None:
        """Stop the periodic status thread."""
        self._status_event.clear()
        if self._status_thread and self._status_thread.is_alive():
            self._status_thread.join(timeout=1.0)
        self._status_thread = None

    def _status_loop(self) -> None:
        """Background: emit a status line every _STATUS_INTERVAL seconds."""
        while self._status_event.is_set():
            time.sleep(5.0)  # check every 5s
            if not self._status_event.is_set():
                break
            now = time.monotonic()
            if now - self._last_status_time >= self._STATUS_INTERVAL:
                self._last_status_time = now
                self._print_status_line()

    def _print_status_line(self) -> None:
        """Print a compact status line showing current state."""
        c = self.c
        elapsed = time.monotonic() - self._stats["total_start"] if self._stats["total_start"] else 0

        parts = [f"{c.SEPARATOR}  ─── "]
        parts.append(f"{c.DIM}{_fmt_elapsed(elapsed)} elapsed{c.SEPARATOR}")

        if self._current_phase:
            parts.append(f" │ {c.CYAN}{self._current_phase}{c.SEPARATOR}")

        # Worker progress
        if self._workers:
            done = sum(1 for v in self._workers.values() if v == "done")
            active = sum(1 for v in self._workers.values() if v == "active")
            total = len(self._workers)
            fraction = done / total if total else 0
            parts.append(f" │ {c.DIM}{_bar(fraction, 12)} {done}/{total}")
            if active:
                parts.append(f" ({active} active)")
            parts.append(c.SEPARATOR)

        # Token count
        tok_total = self._stats["tokens_in"] + self._stats["tokens_out"]
        if tok_total:
            parts.append(f" │ {c.DIM}{tok_total:,} tokens{c.SEPARATOR}")

        parts.append(f" ───{c.RESET}")
        self._write("".join(parts))

    # ------------------------------------------------------------------
    # Start / banner / summary
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Record the total run start time and print the banner."""
        self._stats["total_start"] = time.monotonic()
        self._start_status_ticker()

    def banner(self, mode: str = "", iterations: int = 0, target: str = "") -> None:
        """Print the Spark startup banner."""
        c = self.c
        self._write("")
        self._write(f"  {c.HEADER}⚡ Spark{c.RESET}")
        details = []
        if mode:
            details.append(f"mode={c.ACCENT}{mode}{c.RESET}")
        if iterations:
            details.append(f"iterations={c.ACCENT}{iterations}{c.RESET}")
        if target:
            short = os.path.basename(target.rstrip("/\\")) if target else ""
            details.append(f"target={c.ACCENT}{short}{c.RESET}")
        if details:
            self._write(f"  {c.DIM}{' · '.join(details)}{c.RESET}")
        self._write("")

    def summary(
        self,
        areas_completed: int = 0,
        areas_failed: int = 0,
        areas_skipped: int = 0,
    ) -> None:
        """Print the final run summary."""
        self._stop_status_ticker()

        total = time.monotonic() - self._stats["total_start"] if self._stats["total_start"] else 0
        c = self.c

        self._write(f"\n{c.HEADER}{'═' * 60}{c.RESET}")
        self._write(f"  {c.HEADER}⚡ Spark Run Complete{c.RESET}")
        self._write(f"{c.HEADER}{'═' * 60}{c.RESET}")

        self._write(f"  {c.STAT_LABEL}Total time       {c.STAT_VALUE}{_fmt_elapsed(total)}{c.RESET}")
        self._write(f"  {c.STAT_LABEL}LLM calls        {c.STAT_VALUE}{self._stats['llm_calls']}{c.RESET}")

        tok_in = self._stats["tokens_in"]
        tok_out = self._stats["tokens_out"]
        if tok_in or tok_out:
            self._write(
                f"  {c.STAT_LABEL}Tokens           {c.STAT_VALUE}{tok_in + tok_out:,}"
                f" {c.DIM}({tok_in:,} in / {tok_out:,} out){c.RESET}"
            )

        self._write(f"  {c.STAT_LABEL}Tool calls       {c.STAT_VALUE}{self._stats['tool_calls']}{c.RESET}")

        if areas_completed or areas_failed or areas_skipped:
            self._write("")
            self._write(f"  {c.STAT_LABEL}Areas documented {c.SUCCESS}{areas_completed}{c.RESET}")
            if areas_failed:
                self._write(f"  {c.STAT_LABEL}Areas failed     {c.ERROR}{areas_failed}{c.RESET}")
            if areas_skipped:
                self._write(f"  {c.STAT_LABEL}Areas skipped    {c.DIM}{areas_skipped}{c.RESET}")

        if self._stats["phases"]:
            self._write(f"\n  {c.DIM}Phase breakdown:{c.RESET}")
            for p in self._stats["phases"]:
                name = p["name"]
                el = _fmt_elapsed(p["elapsed"])
                self._write(f"    {c.DIM}{name:30s} {el}{c.RESET}")

        if self._stats["errors"]:
            self._write(f"\n  {c.ERROR}Errors ({len(self._stats['errors'])}):{c.RESET}")
            for err in self._stats["errors"]:
                self._write(f"    {c.RED}✗ {err}{c.RESET}")

        self._write(f"{c.HEADER}{'═' * 60}{c.RESET}\n")

    def cleanup(self) -> None:
        """Stop any running spinner/ticker. Called on exit."""
        self._stop_status_ticker()
        if self._spinner_event.is_set():
            self._spinner_event.clear()
            if self._spinner_thread and self._spinner_thread.is_alive():
                self._spinner_thread.join(timeout=0.3)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

ui = SparkUI()
atexit.register(ui.cleanup)
