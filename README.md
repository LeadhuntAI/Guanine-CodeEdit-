# Guanine (CodeEdit)

A **sandboxed code review system for AI coding agents** with a Flask web UI.

AI coding agents work on isolated file copies in sandboxed workspaces. Humans review every change through an interactive merge UI with side-by-side diffs and hunk-level accept/reject/edit. Nothing touches the original repo until explicitly approved.

---

## Quick Start

```bash
# 1. Clone & install
git clone https://github.com/LeadhuntAI/Guanine-CodeEdit-.git
cd Guanine-CodeEdit-
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install flask mcp

# 2. Register your repo
python setup_sandbox.py

# 3. Start the review server
python file_merger.py
# Open http://localhost:5000
```

---

## How It Works

```
Agent gets task ──> Creates session ──> Checks out files ──> Edits workspace copies
                                                                      │
User reviews in web UI <── Hunk-level accept/reject <── Agent signals done
         │
         └──> Approved changes merged back to repo
```

1. **Register** a repository — Guanine tracks it for agent sessions
2. **Agents work in isolation** — every edit happens on a copy in `sessions/agent_<id>/workspace/`
3. **Review everything** — side-by-side diffs, hunk-level merge editor, inline editing
4. **Merge back** — only approved changes touch the original repo

---

## Claude Code Integration

Guanine can sandbox Claude Code itself via an MCP server + hooks. Claude Code creates agent sessions, checks out files, edits workspace copies, and signals done — all automatically. You review every line before it hits your codebase.

### Setup

```bash
# 1. Register repo + enable sandbox
python setup_sandbox.py

# 2. Configure MCP server (already done via .mcp.json)
# Claude Code auto-discovers it on startup

# 3. Restart Claude Code in this directory
```

### How It Works with Claude Code

When the sandbox is active (`.claude/sandbox-active` exists):

- Claude Code's `Edit`/`Write` tools are **blocked** for all project files
- Claude Code uses MCP tools to create sessions and checkout files
- Claude Code edits workspace copies using its native tools (under `sessions/`)
- You review changes at http://localhost:5000/agent/sessions

### Toggle the Sandbox

| Command | What Happens |
|---------|-------------|
| "sandbox off" | Disables sandbox — Claude Code edits files directly |
| "sandbox on" | Re-enables sandbox — all edits go through review |
| "edit this directly" | One-shot bypass — disables, edits, re-enables |

---

## Features

### Sandboxed Agent Review
- Agents work on isolated file copies, never touching originals
- Track all edits: files modified, lines added/removed, conversation history
- Hunk-level review: accept/reject individual code blocks, or edit inline
- Multi-agent support with color-coded combined diff view
- Auto-compose non-overlapping changes, flag conflicts for manual resolution
- MCP server for integration with Claude Code, Claude Desktop, Cursor, etc.
- Configurable command execution permissions per repo

### File Merge & Conflict Resolution
- Scan multiple source directories, detect conflicts via SHA-256
- Side-by-side diff viewer (unified and split-pane modes)
- Interactive merge editor: pick left/right/both/none per block
- Per-session SQLite persistence (survives app restarts)
- Coverage dashboard with per-source file stats

---

## Agent Integration

### Via MCP Server (Claude Code, Claude Desktop, Cursor)

The MCP server exposes all agent tools. Claude Code discovers it automatically via `.mcp.json`. For other clients:

```json
{
  "mcpServers": {
    "guanine": {
      "command": "python",
      "args": ["/path/to/agent_mcp_server.py"]
    }
  }
}
```

### Via Python Import (co-located agents)

```python
import agent_tools, agent_schema

repo = agent_schema.register_repo('/path/to/repo', 'MyProject')
session = agent_schema.create_session(repo['repo_id'], 'Fix the login bug')
sid, ws, rp = session['session_id'], session['workspace_path'], repo['repo_path']

agent_tools.checkout_file('auth.py', sid, rp, ws)   # get file
# ... edit files in workspace ...
agent_tools.signal_done('Fixed auth bug', sid)       # trigger review
```

### Via Built-in Agentic Engine (OpenRouter API)

```python
import agent_workflow
from agentic.engine import OpenRouterClient, run_workflow

client = OpenRouterClient(api_key="your-key")
registry = agent_workflow.build_tool_registry(session_id, workspace, repo_path)
workflow = agent_workflow.build_workflow("Fix the login bug", model="anthropic/claude-sonnet-4")
run_workflow(workflow, client, registry, {})
```

### Available Tools

| Tool | Description |
|------|-------------|
| `list_repo_files` | Browse original repo files (read-only) |
| `get_repo_file_content` | Read a repo file without checking out |
| `checkout_file` / `checkout_files` | Copy files to workspace for editing |
| `read_file` | Read a workspace file |
| `write_file` | Write to a workspace file (changes tracked) |
| `search_code` | Regex search across workspace files |
| `list_directory` / `get_file_tree` | Browse workspace structure |
| `run_command` | Execute shell commands (permission-controlled) |
| `signal_done` | Mark task complete, trigger review |
| `get_workspace_info` | Get current session's workspace path and status |

---

## Review Workflow

1. Agent signals done — session status changes to **Completed**
2. Open http://localhost:5000/agent/sessions
3. Click **Review Changes** — creates a merge session with workspace vs. original
4. Review each file: side-by-side diff, hunk-level accept/reject, inline editing
5. Click **Accept All** or **Merge Resolved** to write changes back to the repo

### Multi-Agent Combined Diff

When multiple agents edit the same file:
- Each agent's changes shown in a distinct color
- Non-overlapping changes auto-composed
- Overlapping sections flagged as conflicts for manual resolution
- Pairwise tabs for focused per-agent review

---

## Project Structure

```
Guanine(CodeEdit)/
├── file_merger.py              # Core Flask app: scanner, merger, diff, routes
├── agent_schema.py             # Agent session SQLite schema + CRUD
├── agent_tools.py              # Agent tool functions (single source of truth)
├── agent_workflow.py           # Workflow builder, tracked writes, tool registry
├── agent_review.py             # Flask Blueprint for agent UI + review bridge
├── agent_mcp_server.py         # MCP server wrapping agent tools
├── setup_sandbox.py            # One-time repo registration + sandbox setup
├── templates/                  # Jinja2 templates (Bootstrap 5 dark theme)
├── agentic/                    # Lightweight AI workflow engine
│   ├── engine/                 # Runner, loop, OpenRouter client
│   └── tools/                  # Sandboxed filesystem tools
├── sessions/                   # Runtime: SQLite DBs + agent workspaces
└── .claude/                    # Rules, hooks, skills
    ├── hooks/
    │   ├── sandbox-guard.sh    # PreToolUse hook (blocks edits when sandbox active)
    │   └── session-start.sh    # SessionStart hook (injects context)
    └── sandbox-active          # Flag file: sandbox ON when present
```

## Requirements

- Python 3.8+
- `pip install flask mcp`

## License

MIT
