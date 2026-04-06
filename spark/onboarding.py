"""
Spark onboarding -- AI-powered interactive setup for Agent Blueprint.

Handles template installation, repo detection, conversational setup,
and template population through an agentic conversation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from spark.config import SparkConfig
from spark.engine.openrouter import OpenRouterClient
from spark.engine.loop import process_agentic_loop_native
from spark.engine.tool_executor import extract_json
from spark.tools.registry import ToolRegistry
from spark.ui import ui


def run_onboarding(config: SparkConfig, target_dir: str) -> dict:
    """Run the full onboarding flow.

    Returns a repo profile dict with all confirmed settings.
    Keys: project_name, description, scope, language, framework,
          database, package_manager, test_runner, structure,
          templates_installed, claude_md_populated, skip_docs, message
    """
    target = os.path.abspath(target_dir)

    # 1. Detect platform and scan what already exists
    from spark.tools.install_templates import detect_platform, scan_existing
    platform = detect_platform(target)
    existing = scan_existing(target, platform)

    # 2. Determine repo type for the agent's context
    repo_type = _detect_repo_type(target)

    # 3. Run the onboarding agent (it decides about overwriting via user questions)
    ui.phase("Onboarding", f"Platform: {platform}, repo: {repo_type}")
    profile = _run_onboarding_agent(config, target, repo_type, platform, existing)
    ui.phase_end(profile.get("message", "Complete"))
    return profile


def _detect_repo_type(target_dir: str) -> str:
    """Detect whether this is an existing repo, empty, or has spark_init.json.

    Returns: "has_init_json" | "existing" | "empty"
    """
    init_path = os.path.join(target_dir, ".claude", "spark_plans", "spark_init.json")
    if os.path.isfile(init_path):
        try:
            with open(init_path) as f:
                data = json.load(f)
            # Must have at least project_name to count
            if data.get("project_name"):
                return "has_init_json"
        except (json.JSONDecodeError, OSError):
            pass

    # Check if there are source code files (not just .claude/ and templates)
    skip_dirs = {
        ".claude", ".windsurf", ".github", ".codex",
        "agentic", "spark", ".git", "node_modules", "__pycache__", ".venv", "venv",
    }
    try:
        entries = os.listdir(target_dir)
    except OSError:
        return "empty"

    for entry in entries:
        if entry in skip_dirs or entry.startswith("."):
            continue
        entry_path = os.path.join(target_dir, entry)
        if os.path.isfile(entry_path):
            ext = os.path.splitext(entry)[1].lower()
            if ext in {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php",
                       ".c", ".cpp", ".cs", ".swift", ".kt", ".scala"}:
                return "existing"
        elif os.path.isdir(entry_path):
            # Has subdirectories beyond templates -- likely has code
            return "existing"

    return "empty"


def _run_onboarding_agent(
    config: SparkConfig,
    target_dir: str,
    repo_type: str,
    platform: str = "claude",
    existing: dict | None = None,
) -> dict:
    """Run the onboarding agentic loop."""
    # Load agent definition
    agents_dir = os.path.join(os.path.dirname(__file__), "agents")
    with open(os.path.join(agents_dir, "onboarding.json")) as f:
        agent_def = json.load(f)

    # Load system prompt
    prompt_file = agent_def.get("system_prompt_file", "prompts/onboarding.md")
    prompt_path = os.path.join(os.path.dirname(__file__), prompt_file)
    with open(prompt_path) as f:
        system_prompt = f.read()

    # Set up client and tools
    model = config.models.get("onboarding", agent_def["model"])
    client = OpenRouterClient(api_key=config.api_key)
    tool_registry = ToolRegistry(
        definitions_path=os.path.join(os.path.dirname(__file__), "tools", "definitions.json"),
        base_dir=target_dir,
        db=None,  # No DB during onboarding
    )

    # Get tool callables -- returns {name: callable} dict expected by the loop
    tool_names = agent_def.get("tools", [])
    tool_callables = tool_registry.get_tool_callables(tool_names)

    # Build the layer definition for process_agentic_loop_native
    # Note: the loop reads "user_message" (not "content") for the initial prompt
    layer_def = {
        "model": model,
        "system_message": system_prompt,
        "user_message": (
            f"Please set up Agent Blueprint for this repository.\n\n"
            f"Repo type: {repo_type}\n"
            f"Target directory: {target_dir}\n"
            f"Platform: {platform}\n"
            f"Existing state: {json.dumps(existing or {})}\n\n"
            f"Start by using the appropriate tools to understand the repository, "
            f"then guide the user through setup."
        ),
        "max_iterations": agent_def.get("max_iterations", 30),
        "temperature": agent_def.get("temperature", 0.3),
        "max_tokens": agent_def.get("max_tokens", 8192),
    }

    # Create a minimal session with the interface the loop expects
    class _Session:
        def __init__(self):
            self.chat_history = []
            self.context = {}

    session = _Session()
    knowledge = {"rules_text": "", "rules_index": "", "skills_index": ""}

    # Run the loop
    result = process_agentic_loop_native(
        layer_def=layer_def,
        session=session,
        client=client,
        tool_registry=tool_callables,
        knowledge=knowledge,
    )

    # Extract the profile JSON from the agent's response
    response_text = result.get("response", "")
    profile = extract_json(response_text)

    if profile is None:
        # Fallback: return a basic profile, derive name from directory
        return {
            "project_name": os.path.basename(target_dir) or "Unknown",
            "description": "",
            "skip_docs": False,
            "message": "Onboarding complete (could not parse structured output).",
        }

    return profile
