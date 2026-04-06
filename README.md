# Guanine (CodeEdit)

A multi-source file recovery, merge tool, and **sandboxed coding agent review system** with a Flask web UI.

Guanine scans directories and editor local history (Windsurf, VS Code, Cursor), detects conflicts between file versions, and lets you review side-by-side diffs and merge interactively. It also provides a sandboxed environment where AI coding agents edit file copies in isolated workspaces, and humans review/accept/reject changes through the same merge UI.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/LeadhuntAI/Guanine-CodeEdit-.git
cd Guanine-CodeEdit-

# 2. Install
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install flask

# 3. Run
python file_merger.py
# Open http://localhost:5000
```

That's it. The web UI guides you through setup, scanning, and merging.

---

## Features

### File Recovery & Merge
- Scan multiple source directories and detect identical, unique, and conflicting files via SHA-256
- Auto-detect and extract files from Windsurf, VS Code, and Cursor editor local history
- Side-by-side diff viewer with unified and split-pane modes
- Interactive hunk-level merge editor: pick left/right/both/none per block, or type manual edits
- Accept all, reject all, or resolve conflicts one by one
- Per-session SQLite persistence (survives app restarts)
- Coverage dashboard showing per-source file stats
- Standalone PowerShell script (`restore_deleted_files.ps1`) for CLI-only recovery

### Sandboxed Agent Review System
- Register repositories for AI agents to work on
- Agents work on isolated file copies in workspace directories, never touching originals
- Track all agent edits: files modified, lines added/removed, conversation history
- Review agent changes through the same merge UI (hunk-level accept/reject + inline editing)
- Multi-agent support: multiple agents can work on the same repo in parallel
- Combined diff view: see all agents' changes overlaid with color coding, auto-compose non-overlapping edits, flag conflicts
- MCP server for external agent integration (Claude Desktop, Cursor, etc.)
- Configurable command execution permissions per repo

---

## Detailed Usage

### 1. File Recovery Mode

#### Setup Sources

1. Open `http://localhost:5000`
2. On the **Setup** page, add one or more source directories (folders containing files you want to recover/merge)
3. Set a **target directory** where merged files will be written
4. Click **Scan** to build the file inventory

#### Review & Merge

- **Browse**: Split-pane file browser with tree navigation
- **Inventory**: Full file list with filtering by category (unique/identical/conflict) and sorting
- **Conflicts**: List of files that exist in multiple sources with different content
- **Conflict Detail**: Side-by-side diff between any two versions, select which to keep
- **Merge Editor**: For fine-grained control — pick individual code blocks from each version, or type custom edits
- **Execute Merge**: Copy resolved files to the target directory

#### Editor History Extraction

The **Extract History** page scans your local editor history directories for Windsurf, VS Code, and Cursor. It recovers deleted or modified files from the editor's internal backups, preserving original directory structure.

### 2. Agent Review System

#### Register a Repository

1. Navigate to **Agent > Manage Repos**
2. Enter the path to a codebase you want agents to work on
3. Optionally configure:
   - **Allowed commands**: shell commands agents can run (e.g., `pytest tests/`, `npm test`)
   - **Allow free commands**: toggle to let agents run any command (off by default)

#### Create an Agent Session

1. Go to **Agent > Sessions**
2. Select a repo, describe the task, optionally set model and external context
3. Click **Create Session** — this generates a workspace directory under `sessions/agent_<id>/workspace/`

#### Agent Workflow

Agents interact with the system through tool functions. These tools are available via:

- **Direct Python import** (for agents on the same machine):
  ```python
  import agent_tools
  import agent_schema

  # Create session
  repo = agent_schema.register_repo('/path/to/repo', 'MyProject')
  session = agent_schema.create_session(repo['repo_id'], 'Fix the login bug')
  sid = session['session_id']
  ws = session['workspace_path']
  rp = repo['repo_path']

  # Agent workflow
  agent_tools.list_repo_files('*.py', rp)           # browse repo
  agent_tools.checkout_file('auth.py', sid, rp, ws)  # get file
  # ... edit via agent_tools or agentic engine ...
  agent_tools.signal_done('Fixed auth bug', sid)      # done
  ```

- **MCP Server** (for external agents like Claude Desktop or Cursor):
  ```bash
  # Install MCP SDK
  pip install mcp

  # Run MCP server (stdio transport)
  python agent_mcp_server.py

  # Or HTTP transport
  python agent_mcp_server.py --transport streamable-http --port 8080
  ```

  MCP client config (e.g., Claude Desktop):
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

- **Built-in agentic engine** (uses OpenRouter API):
  ```python
  import agent_workflow
  from agentic.engine import OpenRouterClient, run_workflow

  client = OpenRouterClient(api_key="your-key")
  registry = agent_workflow.build_tool_registry(session_id, workspace, repo_path)
  workflow = agent_workflow.build_workflow("Fix the login bug", model="anthropic/claude-sonnet-4")
  result = run_workflow(workflow, client, registry, {})
  ```

#### Available Agent Tools

| Tool | Description |
|------|-------------|
| `list_repo_files` | Browse original repo files (read-only) |
| `get_repo_file_content` | Read a repo file without checking out |
| `checkout_file` / `checkout_files` | Copy files to workspace for editing |
| `read_file` | Read a workspace file |
| `write_file` | Write to a workspace file (changes tracked automatically) |
| `search_code` | Regex search in workspace |
| `list_directory` / `get_file_tree` | Browse workspace structure |
| `run_command` | Execute shell commands (permission-controlled) |
| `signal_done` | Mark task complete, trigger review |

#### Review Agent Changes

1. When an agent signals done, the session status changes to **Completed**
2. Click **Review Changes** — this creates a merge session comparing workspace files against originals
3. Use the same merge UI to accept/reject changes per file, per hunk, or all at once
4. Click **Merge Resolved** or **Accept All** to write changes back to the repo

#### Multi-Agent Combined Diff

When multiple agents edit the same file:
- Each agent's changes are shown in a distinct color (blue, green, purple, etc.)
- Non-overlapping changes are auto-composed
- Overlapping sections are flagged as conflicts — pick which agent's version to use, keep the original, or edit manually
- Pairwise tabs show individual agent diffs for focused review

Access via: `/agent/combined-diff/<filepath>?sessions=sess1,sess2&repo_id=<id>`

---

## Project Structure

```
Guanine(CodeEdit)/
├── file_merger.py              # Main Flask app: scanner, merger, diff, routes (~2860 lines)
├── agent_schema.py             # Agent session SQLite schema + CRUD
├── agent_tools.py              # Agent tool functions (single source of truth)
├── agent_workflow.py           # Workflow builder, tracked write_file, tool registry
├── agent_review.py             # Flask Blueprint for agent UI + review bridge
├── agent_mcp_server.py         # MCP server wrapping agent tools
├── restore_deleted_files.ps1   # Standalone PowerShell recovery script
├── templates/                  # Jinja2 templates (Bootstrap 5 dark theme)
│   ├── base.html               # Base layout
│   ├── setup.html              # Source/target configuration
│   ├── browse.html             # Split-pane file browser
│   ├── inventory.html          # Full file inventory
│   ├── conflicts.html          # Conflict list
│   ├── conflict_detail.html    # Side-by-side diff viewer
│   ├── merge_editor.html       # Interactive hunk merge editor
│   ├── coverage.html           # Per-source coverage dashboard
│   ├── agent_repos.html        # Repo registration
│   ├── agent_sessions.html     # Agent session dashboard
│   ├── agent_session_detail.html # Session detail + actions
│   ├── agent_conversation.html # Agent conversation viewer
│   └── agent_combined_diff.html # Multi-agent combined diff
├── agentic/                    # Lightweight AI workflow engine
│   ├── engine/                 # Runner, loop, OpenRouter client, knowledge
│   └── tools/                  # Sandboxed filesystem tools
├── sessions/                   # Runtime: per-session SQLite DBs + agent workspaces
└── .claude/                    # AI agent rules, docs, skills
```

## Requirements

- Python 3.8+
- Flask (`pip install flask`)
- MCP SDK (`pip install mcp`) — only needed for the MCP server

## License

MIT
