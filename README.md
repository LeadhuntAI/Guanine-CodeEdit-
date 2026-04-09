# Guanine (CodeEdit)

A **multi-agent coding orchestration platform** with sandboxed review, remote project management, and an IDE-style web UI.

Multiple AI coding agents work in parallel on local or remote projects. Humans review every change through side-by-side diffs with hunk-level accept/reject/edit. Supports git clone, branching, push, and SSH deploy for remote server workflows. Nothing touches the original repo until explicitly approved.

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

# 3. Start the server
python file_merger.py
# Open http://localhost:5000/ide
```

---

## How It Works

```
                            ┌──────────────────────────────────────────────┐
                            │         Guanine IDE (localhost:5000)         │
                            │                                              │
  Add Project ──────────────│  [Project Switcher] [+ Add] [Pull] [main]   │
  (local path or git URL)   │  ┌─────────┬──────────────┬──────────────┐  │
                            │  │Explorer │  Code Editor  │ Agent Chat   │  │
  Create Session ───────────│  │ Files   │              │ (Cascade)    │  │
  (select model + backend)  │  │ Search  │              │              │  │
                            │  │ Agents  │              │ > User: fix  │  │
  Review Changes ───────────│  │ Dash    │  Inline Diff  │ > Agent: ... │  │
  (hunk-level accept/reject)│  │ Settings│              │ > [tool:edit]│  │
                            │  └─────────┴──────────────┴──────────────┘  │
  Push + Deploy ────────────│  [Status Bar]                                │
  (git branch → SSH)        └──────────────────────────────────────────────┘
```

### Core Workflow

1. **Add a project** — local directory path or git URL (cloned automatically)
2. **Create an agent session** — pick a model (GLM 5.1, Claude Opus 4.6, etc.) and backend (OpenCode or builtin)
3. **Agents work in isolation** — each edit happens on a copy in a sandboxed workspace
4. **Monitor in real-time** — dashboard shows all running agents, chat panel streams responses
5. **Review everything** — side-by-side diffs, hunk-level merge editor, inline editing
6. **Merge back** — approved changes land in the repo
7. **Push + Deploy** — for git repos, push to a feature branch and SSH deploy to production

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

### Multi-Agent Orchestration
- **Multiple backends**: OpenCode (Go-based agent server) and builtin (OpenRouter API)
- **Parallel agents**: Each repo gets its own OpenCode server on a dynamic port — run agents on multiple projects simultaneously
- **Model selection**: Configure available models per repo (GLM 5.1, Claude Opus 4.6, Kimi K2.5, or any OpenRouter model)
- **Dashboard**: Real-time overview of all running sessions across all projects
- **Cascade-style chat**: Right-side panel for interacting with agents, streaming responses, and viewing tool calls

### Project Management
- **Project switcher**: Toolbar dropdown to switch between registered projects
- **Local or remote**: Add projects by local path or git URL (auto-cloned)
- **Git integration**: Pull, branch, commit, push from the UI
- **SSH deploy**: After pushing, SSH into a server to run deploy commands (git pull && restart)

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

### Via the IDE (recommended)

Open http://localhost:5000/ide — everything is built in:

1. **Add a project**: Click **+ Add** in the toolbar (or Ctrl+Shift+P). Paste a local path or git URL.
2. **Configure**: Go to repo settings to set your OpenRouter API key, choose default model, and configure deploy settings.
3. **Create session**: From the dashboard or sessions page, select repo, model, and backend.
4. **Monitor**: Dashboard auto-refreshes. Chat panel streams agent responses in real time.
5. **Review & merge**: Inline diff with hunk-level controls. Push to git and deploy via SSH.

### Via OpenCode Backend

OpenCode is a Go-based agentic coding server. Guanine manages it automatically:

1. Go to **Settings** in the IDE sidebar
2. If OpenCode isn't installed, click **Install OpenCode** (uses `npm install -g opencode-ai`)
3. Servers start automatically per-repo on dynamic ports (4096+) when you create a session
4. Each repo gets its own isolated server — multiple projects can run agents in parallel

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
| `spawn_agent` | Create a sub-session for parallel work |

---

## Review Workflow

1. Agent signals done — session status changes to **Completed**
2. Open the IDE or http://localhost:5000/agent/sessions
3. Click **Review Changes** — creates a merge session with workspace vs. original
4. Review each file: side-by-side diff, hunk-level accept/reject, inline editing
5. Click **Accept All** or **Merge Resolved** to write changes back to the repo
6. For git repos: **Push** to create a feature branch, then **Deploy** via SSH

### Multi-Agent Combined Diff

When multiple agents edit the same file:
- Each agent's changes shown in a distinct color
- Non-overlapping changes auto-composed
- Overlapping sections flagged as conflicts for manual resolution
- Pairwise tabs for focused per-agent review

---

## Remote Project Workflow

Guanine supports working on remote servers via git + SSH deploy:

```
1. Add project via git URL  →  Cloned to sessions/repos/
2. Agents work on local clone  →  Sandboxed workspaces
3. Review + merge  →  Changes land in local clone
4. Push  →  Creates branch guanine/<task>-<id>, pushes to origin
5. Deploy  →  SSH into server, runs: cd /app && git pull && systemctl restart myapp
```

### Configure Deploy (in repo settings)

| Setting | Example |
|---------|---------|
| Deploy Host | `myserver.com` |
| Deploy User | `ubuntu` |
| Deploy Command | `cd /app && git pull origin main && systemctl restart myapp` |
| SSH Key Path | `~/.ssh/id_rsa` |

---

## Project Structure

```
Guanine(CodeEdit)/
├── file_merger.py              # Core Flask app: scanner, merger, diff, routes
├── agent_schema.py             # Agent session SQLite schema + CRUD
├── agent_tools.py              # Agent tool functions (single source of truth)
├── agent_workflow.py           # Workflow builder, tracked writes, tool registry
├── agent_review.py             # Flask Blueprint: agent UI, review, chat, git, deploy
├── agent_backends.py           # Backend abstraction: OpenCode + builtin + port manager
├── agent_mcp_server.py         # MCP server wrapping agent tools
├── git_ops.py                  # Git clone/branch/push + SSH deploy operations
├── setup_sandbox.py            # One-time repo registration + sandbox setup
├── templates/                  # Jinja2 templates (Bootstrap 5 dark theme)
│   ├── ide_shell.html          # Full IDE: project switcher, editor, chat, dashboard
│   ├── _chat_panel.html        # Cascade-style agent chat partial
│   ├── _dashboard.html         # Agent dashboard partial
│   └── ...                     # Merge UI, review, settings templates
├── agentic/                    # Lightweight AI workflow engine
│   ├── engine/                 # Runner, loop, OpenRouter client, OpenCode client
│   └── tools/                  # Sandboxed filesystem tools
├── sessions/                   # Runtime: DBs, workspaces, cloned repos
└── .claude/                    # Rules, hooks, skills
    ├── hooks/
    │   ├── sandbox-guard.sh    # PreToolUse hook (blocks edits when sandbox active)
    │   └── session-start.sh    # SessionStart hook (injects context)
    └── sandbox-active          # Flag file: sandbox ON when present
```

## Requirements

- Python 3.8+
- `pip install flask mcp`
- Optional: Node.js (for OpenCode backend — `npm install -g opencode-ai`)
- Optional: git (for remote project support)

## License

MIT
