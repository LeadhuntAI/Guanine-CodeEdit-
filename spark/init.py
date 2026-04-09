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
        choices=["fresh", "fill-gaps", "refresh", "adopt"],
        default="fresh",
        help="Documentation generation mode: fresh (overwrite), fill-gaps (undocumented only), refresh (update stale), adopt (import existing docs + surgical patch)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Number of refinement iterations (default: 1 with code index, 3 without)",
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
        "--index-only",
        action="store_true",
        help="Scan files, build code index, import existing docs — no LLM agents",
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
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Launch the Spark web dashboard (local monitoring UI)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8383,
        help="Dashboard port (default: 8383)",
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

    # --- Dashboard mode (no config/API key needed) ---
    if args.dashboard:
        from spark.dashboard import run_dashboard
        return run_dashboard(args.target_dir, port=args.port)

    # --- Config ---
    # index-only and dashboard don't need an API key
    if args.index_only:
        from spark.config import SparkConfig
        config = SparkConfig(api_key="", target_dir=args.target_dir)
    else:
        try:
            config = load_or_create_config(args.target_dir)
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 1

    # Apply CLI overrides
    config.mode = args.mode
    # With code indexing (jcodemunch), the planner gets real import graphs
    # and dependency data upfront — iterations 2+ add negligible value.
    # Default to 1 iteration unless the user explicitly set --iterations.
    _user_set_iterations = any(
        a in (argv or sys.argv[1:]) for a in ("--iterations",)
    ) or any(
        a.startswith("--iterations=") for a in (argv or sys.argv[1:])
    )
    if _user_set_iterations:
        config.iterations = args.iterations
    elif not args.no_code_index:
        config.iterations = 1
    else:
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
    did_onboarding = False
    if not args.skip_onboarding and not _already_onboarded(args.target_dir):
        from spark.onboarding import run_onboarding
        profile = run_onboarding(config, args.target_dir)
        did_onboarding = True
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

    # Seed template files as pre-documented on first run
    if did_onboarding:
        from spark.tools.install_templates import (
            TEMPLATE_DOC_MAP,
            SELF_DOCUMENTING_TEMPLATES,
            detect_platform,
            PLATFORM_MAP,
        )
        platform = detect_platform(args.target_dir)
        instructions = PLATFORM_MAP[platform]["instructions"]
        # Adjust self-documenting paths for non-Claude platforms
        plat_dir = PLATFORM_MAP[platform]["dir"]
        adjusted_self_doc = [
            p.replace(".claude/", f"{plat_dir}/") if plat_dir != ".claude" else p
            for p in SELF_DOCUMENTING_TEMPLATES
        ]
        seeded = db.seed_template_docs(
            target_dir=args.target_dir,
            doc_map=TEMPLATE_DOC_MAP,
            self_documenting=adjusted_self_doc,
            instructions_file=instructions,
        )
        if seeded:
            ui.info(f"Seeded {seeded} template files as pre-documented")

    # Import existing docs if the repo already has Agent Blueprint docs
    # but this is the first Spark run (no completed doc-generation runs yet)
    if not did_onboarding:
        last_run = db.get_last_run()
        has_prior_docs = last_run is not None and last_run.get("mode") in ("completed", "template-seed", "import-existing")
        # Check if there are any real doc-generation runs
        has_doc_runs = False
        if last_run:
            with db._lock:
                cur = db.conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE mode NOT IN ('template-seed', 'import-existing') AND status = 'completed'"
                )
                has_doc_runs = cur.fetchone()[0] > 0
        if not has_doc_runs:
            from spark.tools.install_templates import detect_platform, PLATFORM_MAP
            platform = detect_platform(args.target_dir)
            plat_dir = PLATFORM_MAP[platform]["dir"]
            if config.mode == "adopt":
                adopted = db.adopt_existing_docs(args.target_dir, plat_dir)
                if adopted:
                    ui.info(f"Adopted {len(adopted)} existing doc files with section tracking")
                    for a in adopted:
                        secs = len(a.get("sections", []))
                        files = len(a.get("covered_files", []))
                        ui._write(f"    {a['area_name']}: {secs} sections, {files} source files")
            else:
                imported = db.import_existing_docs(args.target_dir, plat_dir)
                if imported:
                    ui.info(f"Imported {imported} existing doc files into tracking DB")

    # --- Index-only mode: scan, index, import — no LLM agents ---
    if args.index_only:
        ui.phase("Scan", "Indexing repository files")
        db.scan_files(args.target_dir, exclude_patterns=config.exclude_patterns)
        all_files = db.get_all_files()
        ui.phase_end(f"{len(all_files)} files indexed")

        # Code indexing (jCodeMunch)
        if config.code_index:
            ui.phase("Code Index", "Building symbol index (jCodeMunch)")
            from spark.code_index import index_repo as run_code_index
            idx_result = run_code_index(args.target_dir, exclude_patterns=config.exclude_patterns)
            if idx_result:
                ui.phase_end(
                    f"{idx_result.get('file_count', 0)} files, "
                    f"{idx_result.get('symbol_count', 0)} symbols indexed"
                )
                # Set up MCP config for coding agents
                from spark.code_index import finalize_code_index
                from spark.tools.install_templates import detect_platform
                platform = detect_platform(args.target_dir)
                ci_result = finalize_code_index(args.target_dir, platform)
                if ci_result.get("mcp_config"):
                    ui.info("MCP server configured — Claude Code will have code_search tools")
                if ci_result.get("skill_installed"):
                    ui.info("Code search skill installed")
                if ci_result.get("instructions_injected"):
                    from spark.tools.install_templates import PLATFORM_MAP
                    instr_file = PLATFORM_MAP.get(platform, PLATFORM_MAP["claude"])["instructions"]
                    ui.info(f"jcodemunch usage guide injected into {instr_file}")
            else:
                ui.phase_end("No files to index")

        # Summary
        documented = db.get_documented_files()
        stale = db.get_stale_files()
        c = ui.c
        ui._write(f"\n  {c.ACCENT}Index-only complete{c.RESET}")
        ui._write(f"    Files tracked: {len(all_files)}")
        ui._write(f"    Documented:    {len(documented)}")
        ui._write(f"    Stale:         {len(stale)}")
        if stale:
            ui._write(f"    {c.DIM}Run with --mode refresh to update stale docs{c.RESET}")
        undocumented = len(all_files) - len(documented)
        if undocumented > 0:
            ui._write(f"    Undocumented:  {undocumented}")
            ui._write(f"    {c.DIM}Run with --mode fill-gaps to document remaining files{c.RESET}")
        ui._write("")

        db.close()
        return 0

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
