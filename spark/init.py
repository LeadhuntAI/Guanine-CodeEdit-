"""
Spark CLI entry point.

Usage::

    python spark/init.py [options]

Place the spark/ folder inside any repository and run init.py.
By default it targets the parent directory (the repo root).

Parses command-line arguments, loads configuration, initialises the
database, and hands off to the orchestrator.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import asdict

# Ensure the parent of spark/ is on sys.path so imports work
# when running as `python spark/init.py` from the repo root.
_SPARK_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_SPARK_DIR)  # parent of spark/
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)


def _ensure_dependencies() -> None:
    """Install all missing dependencies at startup."""
    _REQUIRED = [
        ("jinja2", "jinja2>=3.1.0"),
        ("tree_sitter_language_pack", "tree-sitter-language-pack>=0.7.0"),
        ("pathspec", "pathspec>=0.12.0"),
    ]
    missing = []
    for import_name, pip_name in _REQUIRED:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pip_name)

    if missing:
        print(f"Installing dependencies: {', '.join(missing)}...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install"] + missing,
            stdout=subprocess.DEVNULL,
        )
        print("Done.")


_ensure_dependencies()

from spark.config import SparkConfig, load_or_create_config
from spark.db import Database
from spark.orchestrator import Orchestrator
from spark.ui import ui

# Global cancellation event — shared with orchestrator for clean shutdown
shutdown_event = threading.Event()


def _handle_sigint(signum, frame):
    """Handle Ctrl+C: set shutdown event and raise KeyboardInterrupt."""
    shutdown_event.set()
    raise KeyboardInterrupt


# ------------------------------------------------------------------
# Banner
# ------------------------------------------------------------------

_BANNER = r"""
  ____                    _
 / ___| _ __   __ _ _ __| | __
 \___ \| '_ \ / _` | '__| |/ /
  ___) | |_) | (_| | |  |   <
 |____/| .__/ \__,_|_|  |_|\_\
       |_|
"""


def _print_banner(config: SparkConfig) -> None:
    c = ui.c
    print(f"{c.HEADER}{_BANNER}{c.RESET}")
    print(f"  {c.STAT_LABEL}Mode           {c.STAT_VALUE}{config.mode}{c.RESET}")
    print(f"  {c.STAT_LABEL}Target dir     {c.STAT_VALUE}{config.target_dir}{c.RESET}")
    print(f"  {c.STAT_LABEL}Iterations     {c.STAT_VALUE}{config.iterations}{c.RESET}")
    print(f"  {c.STAT_LABEL}Max workers    {c.STAT_VALUE}{config.max_concurrent_workers}{c.RESET}")
    if config.exclude_patterns:
        print(f"  {c.STAT_LABEL}Exclude        {c.DIM}{', '.join(config.exclude_patterns)}{c.RESET}")
    print(f"  {c.STAT_LABEL}Code index     {c.STAT_VALUE}{'enabled' if config.code_index else 'disabled'}{c.RESET}")
    print(f"  {c.STAT_LABEL}Models{c.RESET}")
    for role, model in config.models.items():
        short = model.rsplit("/", 1)[-1] if "/" in model else model
        print(f"    {c.DIM}{role:20s}{c.RESET} {c.ACCENT}{short}{c.RESET}")
    print()


# ------------------------------------------------------------------
# Argument parsing
# ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spark",
        description="Spark — multi-agent documentation generator for codebases.",
    )
    parser.add_argument(
        "--mode",
        choices=["fresh", "fill-gaps", "refresh"],
        default="fresh",
        help="Documentation generation mode (default: fresh)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Number of refinement iterations (default: 3)",
    )
    parser.add_argument(
        "--target-dir",
        default=None,
        help="Target repository directory (default: parent of spark/ folder)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=10,
        help="Max concurrent workers (default: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and plan only — don't generate docs",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an interrupted run",
    )
    parser.add_argument(
        "--skip-onboarding",
        action="store_true",
        help="Skip the interactive onboarding step",
    )
    parser.add_argument(
        "--library",
        action="store_true",
        help="Browse and install plugins from the Spark library",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show full API requests/responses and write debug log",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude folders/patterns from scanning (repeatable, e.g. --exclude tests --exclude vendor/legacy)",
    )
    parser.add_argument(
        "--no-code-index",
        action="store_true",
        help="Disable jcodemunch code indexing (symbol search won't be available)",
    )
    return parser


# ------------------------------------------------------------------
# Onboarding check
# ------------------------------------------------------------------

def _already_onboarded(target_dir: str) -> bool:
    """Check if onboarding was already done (spark.db exists with a completed run).

    Checks all supported platform directories (.claude, .windsurf, .github, .codex).
    """
    for plat_dir in (".claude", ".windsurf", ".github", ".codex"):
        db_path = os.path.join(target_dir, plat_dir, "spark.db")
        if not os.path.isfile(db_path):
            continue
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            cursor = conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'completed'")
            count = cursor.fetchone()[0]
            conn.close()
            if count > 0:
                return True
        except Exception:
            continue
    return False


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve target directory: default to parent of spark/ folder
    if args.target_dir is None:
        args.target_dir = _REPO_DIR
    args.target_dir = os.path.abspath(args.target_dir)

    # --- Config ---
    try:
        config = load_or_create_config(args.target_dir)
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 1

    # Apply CLI overrides
    config.mode = args.mode
    config.iterations = args.iterations
    config.target_dir = args.target_dir
    config.max_concurrent_workers = args.max_workers
    if args.exclude:
        # Merge CLI excludes with any from config file (dedup)
        existing = set(config.exclude_patterns)
        for pat in args.exclude:
            existing.add(pat)
        config.exclude_patterns = sorted(existing)

    if args.no_code_index:
        config.code_index = False

    if args.debug:
        from spark.debug import enable_debug
        enable_debug(args.target_dir)

    _print_banner(config)
    ui.start()

    # --- Library mode ---
    if args.library:
        from spark.library import run_library_browser
        return run_library_browser(config, args.target_dir)

    # --- Onboarding ---
    if not args.skip_onboarding and not _already_onboarded(args.target_dir):
        from spark.onboarding import run_onboarding
        profile = run_onboarding(config, args.target_dir)
        if profile.get("skip_docs"):
            print("No code to document yet. Run again after writing some code.")
            return 0
        # Pick up exclude patterns from onboarding
        onboarding_excludes = profile.get("exclude_patterns", [])
        if onboarding_excludes:
            existing = set(config.exclude_patterns)
            for pat in onboarding_excludes:
                existing.add(pat)
            config.exclude_patterns = sorted(existing)

    # --- Database ---
    db = Database(args.target_dir)

    # --- Orchestrator ---
    signal.signal(signal.SIGINT, _handle_sigint)
    orchestrator = Orchestrator(config, db, shutdown_event=shutdown_event)

    try:
        result = orchestrator.run(dry_run=args.dry_run, resume=args.resume)
        if result.get("dry_run"):
            print("Dry run complete.")
        else:
            print(f"Run #{result.get('run_id', '?')} {result.get('status', 'finished')}.")
    except KeyboardInterrupt:
        ui.cleanup()
        print("\n\nInterrupted. Use --resume to continue.")
    except Exception as exc:
        print(f"\nError: {exc}")
        db.close()
        return 1

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
