"""
This is your project's agentic engine. Customize as needed.

Key exports
-----------
OpenRouterClient        HTTP client for the OpenRouter API
LightweightWorkflowSession   Accumulated state for a multi-layer workflow
run_workflow            Execute a full workflow JSON definition
process_layer           Execute a single layer within a workflow
extract_json            Pull structured JSON from LLM text responses
execute_tool_call       Safe tool dispatch with signature filtering
process_agentic_loop    ReAct-style agentic loop
process_agentic_loop_native   Native tool-calling agentic loop
resolve_knowledge       Assemble rules / skills / knowledge for prompts
extract_frontmatter     Parse YAML-like frontmatter from markdown files
"""

from .openrouter import OpenRouterClient
from .runner import (
    LightweightWorkflowSession,
    process_layer,
    run_workflow,
)
from .tool_executor import (
    execute_tool_call,
    extract_json,
    parse_tool_args,
)
from .loop import (
    process_agentic_loop,
    process_agentic_loop_native,
    summarize_loop,
)
from .knowledge import (
    extract_frontmatter,
    resolve_knowledge,
    build_rules_index,
    build_skills_index,
    discover_skills,
)

__all__ = [
    "OpenRouterClient",
    "LightweightWorkflowSession",
    "process_layer",
    "run_workflow",
    "execute_tool_call",
    "extract_json",
    "parse_tool_args",
    "process_agentic_loop",
    "process_agentic_loop_native",
    "summarize_loop",
    "extract_frontmatter",
    "resolve_knowledge",
    "build_rules_index",
    "build_skills_index",
    "discover_skills",
]
