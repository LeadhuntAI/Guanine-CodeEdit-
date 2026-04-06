---
name: json-definitions
description: >
  How to create workflows, tools, and skills via JSON definitions. Covers
  run_workflow, layer types, agentic loop configuration, tool definitions,
  and template processing.
---

# JSON Definitions — Creating Workflows & Tools from Code

All execution is driven by JSON workflow definitions processed by the engine in `agentic/engine/runner.py`. This doc covers how to build agents programmatically.

> **Do NOT read the source code.** This documentation is self-contained. You do not need to open the engine Python files to create workflows or tools. Follow the JSON structures and examples below. If something is unclear, ask the user.

---

## 1. Running a Workflow

```python
from agentic.engine import run_workflow, OpenRouterClient

client = OpenRouterClient(api_key="your-key")
result = run_workflow(workflow_json, client, tool_registry, input_data={"company_name": "Acme"})
```

The `run_workflow` function executes all layers in sequence using an in-memory `LightweightWorkflowSession`. No database is needed.

---

## 2. Workflow JSON Structure

```json
{
  "name": "my-workflow",
  "description": "Optional description",
  "layers": [
    { "layer_type": "prompt", ... },
    { "layer_type": "agentic_loop", ... },
    { "layer_type": "smart_template", ... },
    { "layer_type": "tool", ... }
  ]
}
```

---

## 3. Layer Types

### 3a. `prompt` — Single LLM Call

```json
{
  "layer_type": "prompt",
  "name": "Summariser",
  "model": "openai/gpt-4o-mini",
  "system_message": "You are a summarisation assistant.",
  "temperature": 0.6,
  "prompt": "Summarise the following: {PLO}"
}
```

- **`prompt`** — Rendered through Jinja2, then placeholder replacement. See **section 4** for full details on `{{ variable }}` syntax and placeholders like `{PLO}`.

### 3b. `agentic_loop` — Autonomous Agent with Tools

```json
{
  "layer_type": "agentic_loop",
  "name": "Research Agent",
  "model": "anthropic/claude-sonnet-4-20250514",
  "system_message": "You research codebases thoroughly.",
  "temperature": 0.0,
  "user_message": "Analyze the project structure and identify key modules.",
  "loop_mode": "native",
  "max_iterations": 20,
  "knowledge": {
    "rules": ["rules/docs/coding-standards.md"],
    "skills": ["example-skill"],
    "knowledge_set": ["rules/docs/"],
    "base_dir": "."
  }
}
```

**Tool resolution:** Tools are provided as a `tool_registry` dict when calling `run_workflow()`. The registry maps tool names to callable functions.

**Knowledge fields (see `knowledge-system.md`):**
- **`rules`** — File paths injected as full content into the system message.
- **`knowledge_set`** — File paths whose summaries appear as an `<AVAILABLE_RULES>` index.
- **`skills`** — Skill names whose summaries appear as an `<AVAILABLE_SKILLS>` index.

**Execution modes:**
- **`loop_mode: "native"`** — Uses OpenAI function-calling format (`process_agentic_loop_native`).
- **`loop_mode: "react"`** (default) — Uses ReAct-style text parsing (`process_agentic_loop`).

### 3c. `smart_template` — Template Rendering (No AI)

```json
{
  "layer_type": "smart_template",
  "name": "Context Builder",
  "prompt": "Project: {{ project_name }}\nFiles analyzed: {{ file_count }}"
}
```

Renders Jinja2 template tags against the workflow session context. Does not call the AI API.

### 3d. `tool` — Direct Tool Execution

```json
{
  "layer_type": "tool",
  "tool_name": "get_file_tree",
  "tool_args": {"max_depth": 3}
}
```

Executes a named tool directly as a layer step.

---

## 4. Prompt Processing & Context

All layer types pass their `prompt`/`content` and `system_message` through Jinja2 template rendering before execution. This means you can use `{{ variable }}` syntax and it will be resolved automatically.

### 4a. Passing Input / Context

```python
result = run_workflow(wf_json, client, tools, input_data={"company_name": "Acme", "target_role": "CTO"})
```

The `input_data` dict is merged into the session context. Its keys become available as `{{ company_name }}`, `{{ target_role }}`, etc. in templates.

### 4b. Available Template Variables

| Variable | Contains |
|----------|----------|
| `{{ key }}` | Any key passed via `input_data` dict |
| `{{ last_output }}` | The previous layer's response text |
| `{{ layer_N_output }}` | Output from layer N (0-based) |

Jinja2's full template syntax is available: `{% if %}`, `{% for %}`, filters, etc.

### 4c. Placeholder System (post-template)

After Jinja2 rendering, these string placeholders are resolved:

| Placeholder | Resolves To |
|-------------|-------------|
| `{PLO}` | Previous layer's response text |
| `{OutputLayer-N}` | Response from layer N (0-based) |

**Processing order:** Jinja2 template rendering -> placeholder replacement -> (then AI call or direct output for smart_template).

### 4d. Smart Templates as Context Builders

`smart_template` layers render their content through the same pipeline but **do not call the AI**. Use them to assemble context before an AI layer:

```json
[
  {
    "layer_type": "smart_template",
    "prompt": "Project: {{ project_name }}\nFiles: {{ file_list }}"
  },
  {
    "layer_type": "agentic_loop",
    "user_message": "Using this context:\n{PLO}\n\nAnalyze the project further.",
    "tools": ["read_file", "search_code"]
  }
]
```

The smart_template output becomes the `{PLO}` for the next layer.

---

## 5. Tool Definitions

### 5a. `definitions.json` Format

```json
{
  "tools": [
    {
      "name": "my_tool",
      "description": "Does something useful",
      "function_path": "agentic.tools.my_module.execute",
      "parameters_schema": {
        "type": "object",
        "properties": {
          "query": { "type": "string" },
          "limit": { "type": "integer" }
        },
        "required": ["query"]
      }
    }
  ],
  "tool_sets": {
    "research-tools": ["read_file", "search_code", "my_tool"]
  }
}
```

- **`parameters_schema`** — JSON Schema used for native tool-calling format.
- **`function_path`** — Dotted Python import path to the tool's `execute()` function.

### 5b. Creating a Tool Implementation

Each tool is a Python module with an `execute()` function:

```python
# agentic/tools/my_tool.py
import json

def execute(query: str, limit: int = 10, _base_dir: str = ".", **kwargs) -> str:
    """Does something useful with the query."""
    # Your tool logic here
    result = {"data": f"Results for: {query}", "count": limit}
    return json.dumps(result)
```

Key conventions:
- The function must return a JSON string.
- `_base_dir` is injected by the tool registry (the repo root path).
- Use `**kwargs` to accept and ignore extra arguments gracefully.
- The function's docstring becomes the tool description in ReAct mode.

---

## 6. `LightweightWorkflowSession`

The in-memory session class that tracks state across layers.

Key properties:
- `context` — Shared dict, updated after each layer
- `chat_history` — Full conversation history across all layers
- `outputs` — Per-layer output records (list of dicts with `response` key)
- `current_layer` — Zero-based index of current layer

Returns `session.to_dict()` with `context`, `outputs`, `chat_history`, `current_layer`.
