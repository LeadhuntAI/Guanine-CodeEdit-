"""
Spark Library — interactive plugin browser and installer.

Lets users discover, evaluate, and install plugins from the Spark catalog
through a conversational agent interface.
"""

from __future__ import annotations

import json
import os

from spark.config import SparkConfig
from spark.engine.openrouter import OpenRouterClient
from spark.engine.loop import process_agentic_loop_native
from spark.engine.tool_executor import extract_json
from spark.tools.registry import ToolRegistry
from spark.tools.install_templates import detect_platform


def run_library_browser(config: SparkConfig, target_dir: str) -> int:
    """Run the interactive library browser.

    Returns 0 on success, 1 on error.
    """
    target = os.path.abspath(target_dir)
    platform = detect_platform(target)

    print("\nSpark Plugin Library")
    print("=" * 40)
    print(f"  Platform:   {platform}")
    print(f"  Target dir: {target}")
    print()

    profile = _run_library_agent(config, target, platform)

    installed = profile.get("plugins_installed", [])
    if installed:
        print(f"\n{len(installed)} plugin(s) installed.")
    else:
        print("\nNo plugins installed.")

    if profile.get("message"):
        print(f"{profile['message']}\n")

    return 0


def _run_library_agent(config: SparkConfig, target_dir: str, platform: str) -> dict:
    """Run the library browser agentic loop."""
    # Load agent definition
    agents_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agents")
    with open(os.path.join(agents_dir, "library.json"), encoding="utf-8") as f:
        agent_def = json.load(f)

    # Load system prompt
    prompt_file = agent_def.get("system_prompt_file", "prompts/library.md")
    prompt_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), prompt_file)
    with open(prompt_path, encoding="utf-8") as f:
        system_prompt = f.read()

    # Set up client and tools
    model = config.models.get("library", agent_def["model"])
    client = OpenRouterClient(api_key=config.api_key)
    tool_registry = ToolRegistry(
        definitions_path=os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "tools", "definitions.json"
        ),
        base_dir=target_dir,
        db=None,
    )

    tool_names = agent_def.get("tools", [])
    tool_callables = tool_registry.get_tool_callables(tool_names)

    layer_def = {
        "model": model,
        "system_message": system_prompt,
        "user_message": (
            f"The user wants to browse the Spark plugin library.\n\n"
            f"Platform: {platform}\n"
            f"Target directory: {target_dir}\n\n"
            f"Start by asking what capability they're looking for, "
            f"or offer to show everything available."
        ),
        "max_iterations": agent_def.get("max_iterations", 30),
        "temperature": agent_def.get("temperature", 0.3),
        "max_tokens": agent_def.get("max_tokens", 8192),
    }

    class _Session:
        def __init__(self):
            self.chat_history: list[dict] = []
            self.context: dict = {}

    session = _Session()
    knowledge = {"rules_text": "", "rules_index": "", "skills_index": ""}

    result = process_agentic_loop_native(
        layer_def=layer_def,
        session=session,
        client=client,
        tool_registry=tool_callables,
        knowledge=knowledge,
    )

    response_text = result.get("response", "")
    profile = extract_json(response_text)

    if profile is None:
        return {
            "plugins_installed": [],
            "message": "Library session complete.",
        }

    return profile
