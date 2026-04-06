---
name: agentic-overview
description: >
  High-level architecture overview of the agentic system. Start here to understand
  what the system can do and where to find detailed docs for each area.
---

# Agentic System — Architecture Overview

This is your project's agentic workflow engine. It provides a framework for building autonomous AI agents that can call tools, follow multi-step skills, access curated knowledge, and execute multi-layer workflows defined in JSON.

> **IMPORTANT — Self-contained documentation.**
> These docs (`agentic-overview.md`, `json-definitions.md`, `skills.md`, `knowledge-system.md`, `workflows.md`) contain **all the information you need** to create workflows, skills, tools, and knowledge configurations. **Do NOT open or read** the engine Python source files to understand how the system works internally. The JSON structures, layer types, tool schemas, and skill formats documented here are the authoritative reference. If something is not covered in these docs, ask the user — do not attempt to reverse-engineer it from source code.

## Directory Layout

```
agentic/
├── engine/
│   ├── __init__.py            # Package exports
│   ├── runner.py              # Workflow runner: session, layer dispatch, Jinja2
│   ├── loop.py                # Agentic loop: ReAct and native tool-calling modes
│   ├── tool_executor.py       # Tool dispatch, JSON extraction, arg parsing
│   ├── knowledge.py           # Knowledge resolution, frontmatter, skill discovery
│   └── openrouter.py          # OpenRouter HTTP client (stdlib-only)
├── tools/
│   ├── definitions.json       # Tool schemas (name, description, parameters)
│   ├── read_file.py           # Read file contents
│   ├── write_file.py          # Write file contents
│   ├── list_directory.py      # List directory entries
│   ├── search_code.py         # Regex search in files
│   └── get_file_tree.py       # File tree generation
├── skills/                    # Folder-based skills (SKILL.md packages)
│   └── example-skill/         # Example skill
├── skill_definitions/         # JSON skill definitions (alternative format)
├── workflows/                 # JSON workflow definitions
│   └── example_workflow.json  # Example workflow
└── rules/
    └── docs/                  # Documentation (you are here)
```

## Execution Mode

The engine runs workflows in-memory using `LightweightWorkflowSession`. There is no database requirement — all state is held in Python objects during execution.

```python
from agentic.engine import run_workflow, OpenRouterClient

client = OpenRouterClient(api_key="your-key")
result = run_workflow(workflow_json, client, tool_registry, input_data={"key": "value"})
```

## Skill System

Skills are multi-step procedural definitions that teach agents how to accomplish complex tasks. They live in `agentic/skills/` as folder packages with a `SKILL.md` file, or in `agentic/skill_definitions/` as JSON files.

Skills use progressive disclosure: agents see a summary index in their system prompt, then load full instructions on demand via the `use_skill` tool. See `skills.md` for details.

## Capability Routing

| I want to... | Read |
|---|---|
| Understand the workflow execution flow | `workflows.md` |
| Create a workflow or tools via JSON | `json-definitions.md` |
| Create or use a skill for agents | `skills.md` |
| Give an agent access to rules/knowledge | `knowledge-system.md` |

## Core Tools (included)

| Tool | Purpose |
|------|---------|
| `read_file` | Read file contents by path |
| `write_file` | Write content to a file |
| `list_directory` | List directory entries |
| `search_code` | Regex search across files |
| `get_file_tree` | Get indented file tree |

You can add your own tools by creating a Python module with an `execute()` function and registering it in `tools/definitions.json`.

## Knowledge Injection

Agents can receive curated knowledge in their system prompts via three mechanisms:

- **`rules`** — Full file content injected statically
- **`knowledge_set`** — Summaries shown as `<AVAILABLE_RULES>` index (loaded on demand via `read_rule`)
- **`skills`** — Summaries shown as `<AVAILABLE_SKILLS>` index (loaded on demand via `use_skill`)

See `knowledge-system.md` for the full resolution pipeline.

## Quick Start Example

A minimal JSON workflow with an agentic loop that has tools and knowledge:

```json
{
  "name": "example-agent",
  "layers": [{
    "layer_type": "agentic_loop",
    "model": "anthropic/claude-sonnet-4-20250514",
    "system_message": "You analyze code repositories.",
    "content": "Analyze the project structure and summarize key components.",
    "tools": ["read_file", "get_file_tree"],
    "loop_mode": "native",
    "max_iterations": 15
  }]
}
```

Execute it:

```python
from agentic.engine import run_workflow, OpenRouterClient

client = OpenRouterClient(api_key="your-openrouter-key")
result = run_workflow(workflow_json, client, tool_registry)
print(result["outputs"][-1]["response"])
```
