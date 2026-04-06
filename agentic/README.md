# Agentic Engine

A lightweight, framework-agnostic engine for building autonomous AI agent workflows. This is your project's own copy — customize it freely.

## Overview

The agentic engine lets you define multi-layer workflows in JSON, where each layer can be:

- **`prompt`** — A single LLM call with Jinja2-templated prompts
- **`agentic_loop`** — An autonomous agent that reasons and calls tools iteratively
- **`smart_template`** — A Jinja2 template render (no AI call)
- **`tool`** — A direct tool execution

Workflows execute in-memory with no database required.

## Quick Start

```python
import json
from agentic.engine import run_workflow, OpenRouterClient

# 1. Create the client
client = OpenRouterClient(api_key="your-openrouter-api-key")

# 2. Load a workflow
with open("agentic/workflows/example_workflow.json") as f:
    workflow = json.load(f)

# 3. Build the tool registry
from agentic.tools import read_file, search_code, list_directory, write_file, get_file_tree

tool_registry = {
    "read_file": lambda **kw: read_file.execute(**kw, _base_dir="."),
    "search_code": lambda **kw: search_code.execute(**kw, _base_dir="."),
    "list_directory": lambda **kw: list_directory.execute(**kw, _base_dir="."),
    "write_file": lambda **kw: write_file.execute(**kw, _base_dir="."),
    "get_file_tree": lambda **kw: get_file_tree.execute(**kw, _base_dir="."),
}

# 4. Run it
result = run_workflow(workflow, client, tool_registry, input_data={"target_file": "main.py"})
print(result["outputs"][-1]["response"])
```

## Defining Workflows (JSON)

Create a JSON file in `agentic/workflows/`:

```json
{
  "name": "my-workflow",
  "description": "What this workflow does",
  "layers": [
    {
      "layer_type": "agentic_loop",
      "model": "anthropic/claude-sonnet-4-20250514",
      "system_message": "You are a helpful assistant.",
      "user_message": "Analyze {{ target_file }}",
      "loop_mode": "native",
      "max_iterations": 15
    }
  ]
}
```

See `rules/docs/json-definitions.md` for full layer type reference.

## Defining Tools (JSON + Python)

1. Create a Python module in `agentic/tools/`:

```python
# agentic/tools/my_tool.py
import json

def execute(query: str, _base_dir: str = ".", **kwargs) -> str:
    """Search for something."""
    return json.dumps({"results": [f"Found: {query}"]})
```

2. Register it in `agentic/tools/definitions.json`:

```json
{
  "name": "my_tool",
  "description": "Search for something",
  "function_path": "agentic.tools.my_tool.execute",
  "parameters_schema": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "Search query"}
    },
    "required": ["query"]
  }
}
```

3. Add it to your tool registry when running workflows.

## Defining Skills (SKILL.md)

Create a folder in `agentic/skills/`:

```
agentic/skills/my-skill/
├── SKILL.md
├── references/    (optional)
└── agents/        (optional)
```

`SKILL.md` format:

```markdown
---
name: my-skill
description: >
  What this skill does and when to use it.
allowed-tools:
  - read_file
  - search_code
---

# My Skill

## Phase 1: Do Something
1. Step one...
2. Step two...

## Phase 2: Produce Output
Combine findings...
```

Skills are auto-discovered by the knowledge system. See `rules/docs/skills.md`.

## Running a Workflow

```bash
# From your project root
python -c "
import json
from agentic.engine import run_workflow, OpenRouterClient

client = OpenRouterClient(api_key='...')
with open('agentic/workflows/example_workflow.json') as f:
    wf = json.load(f)
result = run_workflow(wf, client, {}, input_data={'target_file': 'README.md'})
print(result['outputs'][-1]['response'])
"
```

## Customization

This is your engine — customize freely:

- **Add tools:** Create Python modules in `tools/`, register in `definitions.json`
- **Change models:** Update `model` field in workflow layers (any OpenRouter model)
- **Add Jinja2 filters:** Extend the `jinja2.Environment` in `engine/runner.py`
- **Add knowledge:** Create `.md` files with YAML frontmatter, reference them in workflow knowledge configs
- **Create skills:** Add folders to `skills/` with `SKILL.md` files

## Dependencies

- **jinja2** — Template rendering (the only external dependency)
- Python 3.10+ standard library (urllib, json, re, etc.)

## Documentation

Full documentation lives in `rules/docs/`:

| Doc | Covers |
|-----|--------|
| `agentic-overview.md` | Architecture overview, directory layout |
| `json-definitions.md` | Workflow JSON structure, layer types, tool definitions |
| `workflows.md` | Execution model, session state, processing pipeline |
| `knowledge-system.md` | Rules, knowledge sets, skills, prompt injection |
| `skills.md` | Skill format, discovery, sub-agents |
