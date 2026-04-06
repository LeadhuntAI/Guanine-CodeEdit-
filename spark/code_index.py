"""
Code index orchestration — indexes the target repo with jcodemunch
and configures coding agent access (MCP config + skill).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from spark.ui import ui

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# jcodemunch vendor path setup
# ---------------------------------------------------------------------------

_VENDOR_DIR = os.path.join(os.path.dirname(__file__), "vendors", "jcodemunch", "src")


def _ensure_jcodemunch() -> bool:
    """Ensure jcodemunch is importable. Returns True if available."""
    if _VENDOR_DIR not in sys.path:
        sys.path.insert(0, _VENDOR_DIR)

    # Check external deps required by jcodemunch
    _REQUIRED_PACKAGES = [
        ("tree_sitter_language_pack", "tree-sitter-language-pack>=0.7.0"),
        ("pathspec", "pathspec>=0.12.0"),
    ]
    for import_name, pip_name in _REQUIRED_PACKAGES:
        try:
            __import__(import_name)
        except ImportError:
            ui._write(f"  Installing {pip_name.split('>=')[0]}...")
            import subprocess
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", pip_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                logger.warning("Failed to install %s: %s", pip_name, exc)
                return False

    try:
        from jcodemunch_mcp.storage import IndexStore  # noqa: F401
        return True
    except ImportError as exc:
        logger.warning("jcodemunch not available: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

_INDEX_DIR_NAME = ".code-index"


def index_repo(target_dir: str, exclude_patterns: list[str] | None = None) -> Optional[dict]:
    """Index the target repo using jcodemunch.

    Creates or incrementally updates .code-index/ at the repo root.
    Returns the indexing result dict, or None on failure.
    """
    if not _ensure_jcodemunch():
        ui._write("  Code indexing unavailable (missing dependencies)")
        return None

    from jcodemunch_mcp.tools.index_folder import index_folder

    index_path = os.path.join(target_dir, _INDEX_DIR_NAME)

    # Pass patterns that Spark skips but jCodeMunch doesn't have built-in.
    # Central source of truth: spark/ignore.py
    from spark.ignore import JCODEMUNCH_EXTRA_IGNORE
    ignore = list(JCODEMUNCH_EXTRA_IGNORE)
    if exclude_patterns:
        ignore.extend(exclude_patterns)

    try:
        result = index_folder(
            path=target_dir,
            use_ai_summaries=False,
            storage_path=index_path,
            extra_ignore_patterns=ignore,
            incremental=True,
            context_providers=False,
        )
        if not result or not result.get("success"):
            logger.warning("Code indexing returned unsuccessful: %s", result)
            return None
        return result
    except Exception as exc:
        logger.error("Code indexing failed: %s", exc)
        ui._write(f"  Code indexing failed: {exc}")
        return None


def get_repo_identifier(target_dir: str) -> tuple[str, str]:
    """Return (owner, name) for the indexed repo.

    Uses jcodemunch's _local_repo_name which includes a path hash
    to produce a stable identifier like 'local/myrepo-a1b2c3d4'.
    """
    if not _ensure_jcodemunch():
        # Fallback if jcodemunch not available
        name = os.path.basename(os.path.abspath(target_dir)) or "repo"
        return ("local", name)

    from jcodemunch_mcp.tools.index_folder import _local_repo_name
    name = _local_repo_name(Path(os.path.abspath(target_dir)))
    return ("local", name)


def get_index_store(target_dir: str):
    """Return an IndexStore pointing at the repo's .code-index/ directory."""
    if not _ensure_jcodemunch():
        return None

    from jcodemunch_mcp.storage import IndexStore

    index_path = os.path.join(target_dir, _INDEX_DIR_NAME)
    if not os.path.isdir(index_path):
        return None

    return IndexStore(base_path=index_path)


def load_index(target_dir: str):
    """Load the CodeIndex for the target repo. Returns None if not available."""
    store = get_index_store(target_dir)
    if store is None:
        return None

    owner, name = get_repo_identifier(target_dir)
    return store.load_index(owner, name)


# ---------------------------------------------------------------------------
# MCP config for Claude Code
# ---------------------------------------------------------------------------

def write_mcp_config(target_dir: str) -> bool:
    """Write or merge jcodemunch MCP server config into .claude/settings.json.

    Uses the vendored jcodemunch copy via PYTHONPATH so Claude Code can start
    the MCP server without a separate ``pip install jcodemunch-mcp``.

    Returns True if config was written successfully.
    """
    settings_path = os.path.join(target_dir, ".claude", "settings.json")

    # Load existing settings or start fresh
    settings: dict[str, Any] = {}
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Resolve absolute paths for the MCP server config.
    # The vendor src dir is where jcodemunch_mcp lives (not pip-installed).
    vendor_src = os.path.abspath(_VENDOR_DIR)
    storage_path = os.path.abspath(os.path.join(target_dir, _INDEX_DIR_NAME))

    # Merge jcodemunch MCP server entry
    mcp_servers = settings.setdefault("mcpServers", {})
    mcp_servers["jcodemunch"] = {
        "command": sys.executable,
        "args": [
            "-m", "jcodemunch_mcp.server",
            "--storage", storage_path,
        ],
        "env": {
            "PYTHONPATH": vendor_src,
        },
    }

    try:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        return True
    except OSError as exc:
        logger.error("Failed to write MCP config: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Skill for non-MCP platforms
# ---------------------------------------------------------------------------

def install_code_search_skill(target_dir: str, platform: str) -> bool:
    """Install the code-search skill template into the target repo.

    Returns True if the skill was installed successfully.
    """
    from spark.tools.install_templates import PLATFORM_MAP

    info = PLATFORM_MAP.get(platform, PLATFORM_MAP.get("claude", {}))
    platform_dir = info.get("dir", ".claude")

    skill_src = os.path.join(
        os.path.dirname(__file__), "templates", ".claude", "skills", "code-search", "SKILL.md"
    )

    if not os.path.isfile(skill_src):
        logger.warning("Code search skill template not found at %s", skill_src)
        return False

    skill_dst = os.path.join(target_dir, platform_dir, "skills", "code-search", "SKILL.md")

    try:
        os.makedirs(os.path.dirname(skill_dst), exist_ok=True)
        shutil.copy2(skill_src, skill_dst)
        return True
    except OSError as exc:
        logger.error("Failed to install code search skill: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Combined finalize step
# ---------------------------------------------------------------------------

def finalize_code_index(target_dir: str, platform: str) -> dict:
    """Write MCP config and install skill after Spark run completes.

    Returns a summary dict of what was set up.
    """
    result = {"mcp_config": False, "skill_installed": False}

    # MCP config (Claude Code)
    if platform == "claude":
        result["mcp_config"] = write_mcp_config(target_dir)

    # Skill (all platforms including Claude — skill is a fallback)
    result["skill_installed"] = install_code_search_skill(target_dir, platform)

    return result
