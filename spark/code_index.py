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
    """Write jcodemunch MCP server config into .mcp.json and hooks into .claude/settings.json.

    Claude Code reads MCP servers from .mcp.json at the project root.
    Hooks go into .claude/settings.json.

    Uses the vendored jcodemunch copy via PYTHONPATH so Claude Code can start
    the MCP server without a separate ``pip install jcodemunch-mcp``.

    Returns True if config was written successfully.
    """
    # Resolve absolute paths for the MCP server config.
    # Prefer the target repo's own vendored copy if it exists, otherwise
    # fall back to agent-blueprint's copy (the one running right now).
    target_vendor = os.path.join(os.path.abspath(target_dir), "spark", "vendors", "jcodemunch", "src")
    vendor_src = target_vendor if os.path.isdir(target_vendor) else os.path.abspath(_VENDOR_DIR)
    storage_path = os.path.abspath(os.path.join(target_dir, _INDEX_DIR_NAME))
    log_path = os.path.abspath(os.path.join(target_dir, ".claude", "jcodemunch.log"))

    jcodemunch_entry = {
        "command": sys.executable,
        "args": [
            "-m", "jcodemunch_mcp.server",
            "--log-file", log_path,
            "--log-level", "INFO",
        ],
        "env": {
            "PYTHONPATH": vendor_src,
            "CODE_INDEX_PATH": storage_path,
        },
        "instructions": (
            "MANDATORY: Use these tools instead of Read, Grep, Glob, and Bash "
            "(grep, find, cat, head) for ALL code exploration, search, and navigation. "
            "These tools understand code structure (symbols, imports, dependencies, "
            "blast radius) — built-in tools only see raw text. "
            "Use get_file_outline before reading a file. "
            "Use search_symbols instead of Grep for finding definitions. "
            "Use get_symbol_source to read specific functions instead of Read on entire files. "
            "Use get_blast_radius before modifying shared functions."
        ),
    }

    # ---- .mcp.json (MCP server config — what Claude Code reads) ----
    mcp_path = os.path.join(target_dir, ".mcp.json")
    mcp_config: dict[str, Any] = {}
    if os.path.isfile(mcp_path):
        try:
            with open(mcp_path, "r", encoding="utf-8") as f:
                mcp_config = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    mcp_config.setdefault("mcpServers", {})["jcodemunch"] = jcodemunch_entry

    try:
        with open(mcp_path, "w", encoding="utf-8") as f:
            json.dump(mcp_config, f, indent=2)
    except OSError as exc:
        logger.error("Failed to write .mcp.json: %s", exc)
        return False

    # ---- .claude/settings.json (hooks only) ----
    settings_path = os.path.join(target_dir, ".claude", "settings.json")
    settings: dict[str, Any] = {}
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Remove jcodemunch from settings mcpServers if it was there from before
    if "mcpServers" in settings and "jcodemunch" in settings["mcpServers"]:
        del settings["mcpServers"]["jcodemunch"]
        if not settings["mcpServers"]:
            del settings["mcpServers"]

    # Add PostToolUse hook to auto-reindex files after Edit/Write
    _install_reindex_hook(settings, target_dir, vendor_src, storage_path)

    # Add PreToolUse hook to nudge toward jcodemunch for code exploration
    _install_pretool_hook(settings, target_dir)

    try:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        return True
    except OSError as exc:
        logger.error("Failed to write settings.json: %s", exc)
        return False


# ---------------------------------------------------------------------------
# PostToolUse hook: auto-reindex after Edit/Write
# ---------------------------------------------------------------------------

_REINDEX_HOOK_SCRIPT = """\
#!/usr/bin/env bash
# PostToolUse hook: re-index edited file in jcodemunch after Edit/Write.
# Installed by Spark. Runs in ~0.5s per file — invisible to the user.

INPUT=$(cat)

# Extract file_path from the tool input JSON
FILE_PATH=$(python -c "
import sys, json
data = json.load(sys.stdin)
# PostToolUse sends tool_input with file_path
fp = data.get('tool_input', {{}}).get('file_path', '')
if fp:
    print(fp)
" <<< "$INPUT" 2>/dev/null)

# If no file path extracted, skip
[ -z "$FILE_PATH" ] && exit 0

# Only reindex files that exist and are under the project
[ -f "$FILE_PATH" ] || exit 0

# Run jcodemunch index-file in background (non-blocking)
PYTHONPATH="{pythonpath}" CODE_INDEX_PATH="{storage_path}" \\
    python -m jcodemunch_mcp.server index-file "$FILE_PATH" --no-ai-summaries >/dev/null 2>&1 &

exit 0
"""


def _install_reindex_hook(
    settings: dict, target_dir: str, vendor_src: str, storage_path: str,
) -> None:
    """Add the jcodemunch reindex PostToolUse hook to settings and write the script."""
    # Write the hook script
    hooks_dir = os.path.join(target_dir, ".claude", "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "jcodemunch-reindex.sh")

    script = _REINDEX_HOOK_SCRIPT.format(
        pythonpath=vendor_src.replace("\\", "/"),
        storage_path=storage_path.replace("\\", "/"),
    )

    try:
        with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
    except OSError as exc:
        logger.error("Failed to write reindex hook: %s", exc)
        return

    # Merge hook entry into settings — don't clobber existing PostToolUse hooks
    hooks = settings.setdefault("hooks", {})
    post_hooks = hooks.setdefault("PostToolUse", [])

    # Check if our hook already exists (idempotent)
    hook_cmd = f"bash .claude/hooks/jcodemunch-reindex.sh"
    for entry in post_hooks:
        for h in entry.get("hooks", []):
            if h.get("command") == hook_cmd:
                return  # already installed

    post_hooks.append({
        "matcher": "Edit|Write",
        "hooks": [
            {
                "type": "command",
                "command": hook_cmd,
            }
        ],
    })


# ---------------------------------------------------------------------------
# PreToolUse hook: nudge toward jcodemunch for code exploration
# ---------------------------------------------------------------------------

_PRETOOL_HOOK_SCRIPT = """\
#!/usr/bin/env bash
# PreToolUse hook: remind to use jcodemunch MCP tools for code exploration.
# Installed by Spark. Non-blocking — prints a warning, does not abort.

INPUT=$(cat)
TOOL_NAME="$TOOL_NAME"

# Only warn for Read, Grep, Glob on source code files
case "$TOOL_NAME" in
  Read|Grep|Glob) ;;
  *) exit 0 ;;
esac

# Extract the target path from tool input
TARGET=$(python -c "
import sys, json
data = json.load(sys.stdin)
inp = data.get('tool_input', {{}})
# Read uses file_path, Grep/Glob use path or pattern
p = inp.get('file_path', inp.get('path', inp.get('pattern', '')))
print(p)
" <<< "$INPUT" 2>/dev/null)

# Only warn for source code file extensions
case "$TARGET" in
  *.py|*.js|*.ts|*.jsx|*.tsx|*.go|*.rs|*.java|*.rb|*.php|*.cs|*.cpp|*.c|*.h|*.swift|*.kt)
    echo "HINT: jcodemunch MCP tools (search_symbols, get_file_outline, get_symbol_source) are available and understand code structure. Consider using them instead of $TOOL_NAME for code exploration."
    ;;
esac

exit 0
"""


def _install_pretool_hook(
    settings: dict, target_dir: str,
) -> None:
    """Add the jcodemunch PreToolUse hint hook to settings."""
    hooks_dir = os.path.join(target_dir, ".claude", "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "jcodemunch-prefer.sh")

    try:
        with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_PRETOOL_HOOK_SCRIPT)
    except OSError as exc:
        logger.error("Failed to write pretool hook: %s", exc)
        return

    hooks = settings.setdefault("hooks", {})
    pre_hooks = hooks.setdefault("PreToolUse", [])

    hook_cmd = "bash .claude/hooks/jcodemunch-prefer.sh"
    for entry in pre_hooks:
        for h in entry.get("hooks", []):
            if h.get("command") == hook_cmd:
                return  # already installed

    pre_hooks.append({
        "matcher": "Read|Grep|Glob",
        "hooks": [
            {
                "type": "command",
                "command": hook_cmd,
            }
        ],
    })


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
# Instructions file jcodemunch section injection
# ---------------------------------------------------------------------------

# Tool name prefixes vary by platform.  Claude Code uses mcp__<server>__<tool>,
# others may use different conventions or the raw tool name via skill/CLI.
_TOOL_PREFIX = {
    "claude":   "mcp__jcodemunch__",
    "windsurf": "jcodemunch_",
    "copilot":  "jcodemunch_",
    "codex":    "jcodemunch_",
}

# Which instructions file each platform reads on session start.
_INSTRUCTIONS_FILE = {
    "claude":   "CLAUDE.md",
    "windsurf": "AGENTS.md",
    "copilot":  "AGENTS.md",
    "codex":    "AGENTS.md",
}


def _build_jcodemunch_section(platform: str) -> str:
    """Build the markdown section tailored to *platform*'s tool-name convention."""
    from spark.tools.install_templates import PLATFORM_MAP

    info = PLATFORM_MAP.get(platform, PLATFORM_MAP["claude"])
    settings_dir = info["dir"]  # e.g. ".claude", ".windsurf"
    prefix = _TOOL_PREFIX.get(platform, "jcodemunch_")

    # For non-Claude platforms, tools are accessed via the code-search skill / CLI
    # rather than MCP, so phrase the section differently.
    if platform == "claude":
        intro = (
            f"This project has a jcodemunch code index at `.code-index/`. "
            f"An MCP server is configured in `{settings_dir}/settings.json` "
            f"that exposes 45 code analysis tools.\n\n"
            f"**MANDATORY: NEVER use the Read tool, Grep tool, Glob tool, or Bash commands "
            f"(grep, find, cat, head) to explore, search, or navigate code when jcodemunch "
            f"MCP tools are available. The jcodemunch tools understand code structure "
            f"(symbols, imports, dependencies, blast radius) — built-in tools only see raw text. "
            f"Use Read/Grep only for non-code files (config, docs, logs) or when editing.**"
        )
        tool_header = "### Key MCP tools to use"
    else:
        intro = (
            f"This project has a jcodemunch code index at `.code-index/`. "
            f"A code-search skill is installed at `{settings_dir}/skills/code-search/SKILL.md` "
            f"with 45 code analysis commands.\n\n"
            f"**MANDATORY: NEVER use grep, find, or raw file reads to explore, search, or "
            f"navigate code when jcodemunch tools are available. The jcodemunch tools understand "
            f"code structure (symbols, imports, dependencies, blast radius) — text tools only "
            f"see raw text. Use grep/cat only for non-code files (config, docs, logs).**"
        )
        tool_header = "### Key tools to use"

    tools_table = f"""\
| Tool | When to use |
|------|-------------|
| `{prefix}search_symbols` | Finding where something is defined (instead of grep) |
| `{prefix}get_file_outline` | See all symbols in a file before reading it |
| `{prefix}get_symbol_source` | Read just one function/class (instead of reading the whole file) |
| `{prefix}get_blast_radius` | Before modifying a function — see what depends on it |
| `{prefix}get_dependency_graph` | Understand module-level import relationships |
| `{prefix}get_class_hierarchy` | Explore inheritance chains |
| `{prefix}search_text` | Full-text search across indexed files |
| `{prefix}get_file_tree` | Repository file structure |"""

    return f"""\

<!-- jcodemunch-code-index -->
## Code Index (jcodemunch)

{intro}

{tool_header}

{tools_table}

### Workflow

1. **Before reading a file**: use `{prefix}get_file_outline` to see what's in it, then `{prefix}get_symbol_source` for specific symbols
2. **Finding definitions**: use `{prefix}search_symbols` instead of grep
3. **Understanding impact**: use `{prefix}get_blast_radius` before modifying shared functions
4. **Exploring structure**: use `{prefix}get_dependency_graph` and `{prefix}get_class_hierarchy`

### Symbol ID format

Symbol IDs follow `file_path::qualified_name#kind`, e.g.:
- `src/auth.py::login#function`
- `src/models.py::User#class`
- `src/models.py::User.save#method`
<!-- /jcodemunch-code-index -->"""


def inject_instructions_section(target_dir: str, platform: str) -> bool:
    """Inject jcodemunch usage instructions into the platform's instructions file.

    Claude  -> CLAUDE.md
    Windsurf -> AGENTS.md
    Copilot  -> AGENTS.md
    Codex    -> AGENTS.md

    Idempotent — uses HTML comment markers to replace existing section
    or appends if not found.  Returns True if the file was updated.
    """
    import re

    instructions_file = _INSTRUCTIONS_FILE.get(platform, "CLAUDE.md")
    instructions_path = os.path.join(target_dir, instructions_file)

    if not os.path.isfile(instructions_path):
        logger.warning("No %s found at %s — skipping injection", instructions_file, instructions_path)
        return False

    try:
        with open(instructions_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        logger.error("Failed to read %s: %s", instructions_file, exc)
        return False

    section = _build_jcodemunch_section(platform)

    # Replace existing section if present
    pattern = r"<!-- jcodemunch-code-index -->.*?<!-- /jcodemunch-code-index -->"
    if re.search(pattern, content, re.DOTALL):
        new_content = re.sub(
            pattern, section.strip(), content, flags=re.DOTALL,
        )
    else:
        new_content = content.rstrip() + "\n" + section + "\n"

    try:
        with open(instructions_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return True
    except OSError as exc:
        logger.error("Failed to write %s: %s", instructions_file, exc)
        return False


# Backwards compat alias
inject_claude_md_section = inject_instructions_section


# ---------------------------------------------------------------------------
# Combined finalize step
# ---------------------------------------------------------------------------

def finalize_code_index(target_dir: str, platform: str) -> dict:
    """Write MCP config and install skill after Spark run completes.

    Returns a summary dict of what was set up.
    """
    result = {"mcp_config": False, "skill_installed": False, "instructions_injected": False}

    # MCP config (Claude Code)
    if platform == "claude":
        result["mcp_config"] = write_mcp_config(target_dir)

    # Skill (all platforms including Claude — skill is a fallback)
    result["skill_installed"] = install_code_search_skill(target_dir, platform)

    # Inject jcodemunch guidance into instructions file (all platforms)
    result["instructions_injected"] = inject_instructions_section(target_dir, platform)

    return result
