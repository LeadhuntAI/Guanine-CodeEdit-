---
name: workflows
description: >
  Workflow engine documentation. Covers the execution model, session state,
  layer types, layer processing, and the full execution flow.
---

# Workflow Engine Documentation

This document describes the workflow engine that powers the agentic system. It covers the execution model, session state management, layer processing, and the functions that drive execution.

---

## 1. High-Level Architecture

A **workflow** is a sequence of **layers** defined in JSON. Each layer wraps exactly one executable element: a `prompt`, `agentic_loop`, `smart_template`, or `tool`. When a workflow runs, a `LightweightWorkflowSession` is created to track state, and layers are processed one-by-one in order. Each layer produces an output that is logged on the session, and the accumulated context dict is passed forward to subsequent layers.

```
run_workflow(workflow_json, client, tool_registry, input_data)
  +-> creates LightweightWorkflowSession
        +-> loops through layers (0-indexed)
              +-> each process_layer(layer_def, session, client, tool_registry)
                    +-> calls AI API / executes tool / renders template
                    +-> logs output on session
                    +-> merges response into session.context
```

---

## 2. Session State

### `LightweightWorkflowSession`

The in-memory session class that tracks all state during workflow execution.

| Attribute | Type | Description |
|---|---|---|
| `context` | `dict` | Shared context dict — updated after each layer completes |
| `chat_history` | `list[dict]` | Full conversation history across all layers |
| `outputs` | `list[dict]` | Per-layer output records |
| `current_layer` | `int` | Zero-based index of the layer currently executing |

**Methods:**

- **`add_input(obj)`** — Merge a dict into the session context.
- **`get_input()`** — Return a shallow copy of the current context.
- **`log_output(**kwargs)`** — Record the output of the current layer.
- **`to_dict()`** — Serialise the full session state.

---

## 3. Layer Types

### 3.1 `prompt` — Single LLM Call

Sends a prompt to an LLM and returns the response.

**Processing:**
1. Prompt text is resolved through placeholder replacement (`{PLO}`, `{OutputLayer-N}`).
2. Prompt text is rendered through Jinja2 with session context.
3. System message (if provided) goes through the same pipeline.
4. Messages are sent to the LLM via `OpenRouterClient.chat_completion()`.

### 3.2 `agentic_loop` — Autonomous Agent with Tools

An autonomous reasoning agent with tool-calling capability. The agent iterates (up to `max_iterations`, default 20) until it produces a final answer.

**Two execution modes:**
- **ReAct mode** (`loop_mode: "react"`) — Agent uses Thought/Action/Observation text format. Responses are parsed for `Action:` + `Action Input:` (tool call) or `Answer:` (final result).
- **Native mode** (`loop_mode: "native"`) — Uses OpenAI function-calling format. The LLM returns structured `tool_calls` objects.

**Execution flow:**
1. System message is assembled from: base prompt + knowledge (rules, rules index, skills index) + tool descriptions + format instructions.
2. Agent loop runs: LLM responds -> parse for tool call or answer -> execute tool if needed -> feed observation back -> repeat.
3. On completion, the loop chat history is summarized.

### 3.3 `smart_template` — Template Rendering (No AI)

Renders Jinja2 template text against the session context. Does not call any AI API. Use for assembling structured context before an AI layer.

### 3.4 `tool` — Direct Tool Execution

Executes a named tool directly. The tool name and arguments are specified in the layer definition.

---

## 4. Core Functions

### 4.1 `run_workflow(workflow_json, client, tool_registry, input_data)`

Main entry point. Creates a session, seeds it with `input_data`, and processes each layer in sequence.

### 4.2 `process_layer(layer_def, session, client, tool_registry)`

Dispatches a single layer based on its `layer_type`. Returns a dict with at least a `response` key.

### 4.3 Prompt Processing Pipeline

1. **Placeholder replacement** — `{PLO}` becomes previous layer output, `{OutputLayer-N}` becomes output from layer N.
2. **Jinja2 rendering** — `{{ variable }}` syntax resolved against `session.context`.
3. **LLM call** — Processed prompt sent to `OpenRouterClient.chat_completion()`.

### 4.4 Agentic Loop Processing

`process_agentic_loop(layer_def, session, client, tool_registry, knowledge)`:
1. Builds comprehensive system message with tool descriptions and knowledge.
2. Runs agent loop with iteration limit.
3. Parses responses for tool calls or final answer.
4. Executes tool calls via `execute_tool_call()`.
5. Summarizes the loop conversation on completion.

### 4.5 Tool Execution

`execute_tool_call(available_tools, tool_name, tool_args)`:
1. Looks up the tool function in the registry.
2. Parses arguments (handles JSON strings or dicts).
3. Filters arguments to match the function's actual signature.
4. Executes and returns a JSON string result.

---

## 5. JSON Extraction

`extract_json(response)` extracts structured JSON from LLM responses:
1. Removes `<think>` blocks (reasoning traces).
2. Finds ` ```json ``` ` fenced code blocks (uses the last one).
3. Falls back to brace-matching to find the outermost `{...}`.

---

## 6. Execution Flow Summary

1. **`run_workflow()`** — Creates `LightweightWorkflowSession`, seeds with `input_data`.
2. **Layer loop** — Iterates through each layer definition in order.
3. **`process_layer()`** — Dispatches to the appropriate handler based on `layer_type`.
4. **Handler** — Processes the layer (AI call, tool execution, or template render).
5. **Output** — Result stored in `session.outputs`, response merged into `session.context`.
6. **Return** — `session.to_dict()` with full `context`, `outputs`, `chat_history`.

---

## 7. Important Patterns

- **Context propagation:** Each layer's response is stored as `layer_N_output` and `last_output` in the session context, making it available to subsequent layers via Jinja2 (`{{ last_output }}`) or placeholders (`{PLO}`).
- **Tool safety:** `execute_tool_call()` never raises — errors are returned as JSON `{"error": "..."}` strings.
- **Argument filtering:** Tool arguments are filtered to match the function's actual signature via `inspect.signature()`, preventing unexpected keyword arguments.
- **Jinja2 safety:** Template errors return the unprocessed text rather than raising, so workflows are resilient to minor template issues.
