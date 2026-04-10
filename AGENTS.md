# AGENTS.md — Guanine (CodeEdit) Instructions for OpenCode

## CRITICAL: Sandboxed Editing — MANDATORY

You are running inside Guanine CodeEdit, a sandboxed code review system. **You MUST use the Guanine MCP tools for ALL file modifications.** Direct file editing is PROHIBITED.

### Required Workflow

1. **At session start:** Call `guanine.activate_for_backend_session` with your session ID, OR `guanine.activate_session` with the `GUANINE_SESSION_ID` from your environment.
2. **Before editing any file:** Call `guanine.checkout_file` for EVERY file you want to modify.
3. **To write changes:** Use `guanine.write_file` — this writes to your sandbox workspace, NOT the repo.
4. **When finished:** Call `guanine.signal_done` with a summary of your changes.

### PROHIBITED Actions

- **DO NOT** use your built-in Write, Edit, or Bash tools to modify files in the repository.
- **DO NOT** use `sed`, `awk`, `cat >`, `echo >`, or any shell command to write files.
- **DO NOT** bypass the sandbox by writing directly to the repo path.
- Reading files with your built-in Read tool is OK for context-gathering, but ALL writes MUST go through `guanine.write_file`.

### Why This Matters

A human reviewer will review your changes through Guanine's diff UI before they are merged into the repo. If you write files directly, those changes bypass review and can destroy work in progress. **This has already caused data loss.**

### Available Guanine MCP Tools

| Tool | Purpose |
|------|---------|
| `guanine.activate_for_backend_session` | Bind to your Guanine session |
| `guanine.activate_session` | Activate by Guanine session ID |
| `guanine.checkout_file` | Copy a repo file to your workspace for editing |
| `guanine.checkout_files` | Batch checkout multiple files |
| `guanine.write_file` | Write to your workspace (tracked, diffed) |
| `guanine.read_file` | Read from your workspace |
| `guanine.get_repo_file_content` | Read from the repo (read-only, no checkout) |
| `guanine.list_repo_files` | Discover repo files |
| `guanine.search_code` | Search workspace files |
| `guanine.run_command` | Run shell commands in workspace |
| `guanine.signal_done` | Mark task complete for human review |
| `guanine.get_workspace_info` | Recover workspace path after context loss |

---

## Project Overview

**Guanine (CodeEdit)** — A multi-agent coding orchestration platform with sandboxed review, git-based remote project management, and multi-source file merging. Multiple AI agents (OpenCode, builtin OpenRouter) work in parallel on local or remote projects; humans review changes through side-by-side diffs and hunk-level merge editing. Supports git clone → branch → push → SSH deploy workflows for remote servers.

Stack: Python, Flask, SQLite, Jinja2, Bootstrap 5, pytest

## Architecture

- **File Scanner** (`file_merger.py` — `FileScanner` class) — Walks source directories, computes SHA-256 hashes, detects binary files, categorizes items as unique/identical/conflict
- **Merge Engine** (`file_merger.py` — `MergeEngine` class) — Generates unified and side-by-side diffs, executes file copy/merge operations with skip-if-identical logic
- **Session Persistence** (`file_merger.py` — SQLite layer) — Stores scan results, merge items, file versions, and coverage stats in per-session SQLite databases with WAL mode
- **Flask Web UI** (`file_merger.py` — Flask routes + `templates/`) — Setup wizard, scan progress (SSE), inventory browser, conflict resolution, interactive merge editor, coverage dashboard
- **Agent Review System** (`agent_schema.py`, `agent_tools.py`, `agent_workflow.py`, `agent_review.py`) — Sandboxed agent workspaces, tool exposure via Python/MCP, review bridge to merge UI
- **Agent Backend Abstraction** (`agent_backends.py`) — Pluggable backend system with `BuiltinBackend` (OpenRouter) and `OpenCodeBackend` (HTTP API). Per-repo dynamic port allocation for parallel OpenCode servers. Backend factory `get_backend_for_repo()` configures backends from repo settings.
- **OpenCode Client** (`agentic/engine/opencode_client.py`) — HTTP client for OpenCode server API using stdlib `urllib`. Handles health checks, auto-start as subprocess, session/message management, SSE event streaming.
- **Git Operations** (`git_ops.py`) — Clone, pull, branch, commit, push, and SSH deploy. Supports remote project workflows: clone a git URL → agents work on feature branches → push → SSH deploy to server.
- **MCP Server** (`agent_mcp_server.py`) — Model Context Protocol server for external agent integration
- **HTML Templates** (`templates/`) — Jinja2 templates for merge UI, agent review, IDE shell with project switcher, Cascade-style chat panel, and agent dashboard

## Directory Layout

```
Guanine(CodeEdit)/
├── CLAUDE.md                              <- Instructions for Claude Code (direct editing allowed)
├── AGENTS.md                              <- Instructions for OpenCode (YOU — sandbox enforced)
├── .claude/
│   ├── RULES_INDEX.md                     <- Global rules index
│   ├── rules/
│   │   ├── invariants.md                  <- Design invariants (NEVER violate)
│   │   ├── testing.md                     <- How tests are organized
│   │   ├── bug-fixing.md                  <- Bug fix discipline
│   │   └── docs/                          <- Cross-module documentation rules
│   ├── skills/
│   │   ├── code-documenter/               <- Skill: analyse & document code
│   │   └── code-search/                   <- Skill: search codebase
│   └── tests/
│       ├── <feature>-test-plan.md         <- Active test plans
│       └── history/                       <- Archived completed plans
├── file_merger.py                         <- Core merge app (~2,170 lines)
├── agent_schema.py                        <- Agent session SQLite schema + CRUD
├── agent_tools.py                         <- Agent tool functions (single source of truth)
├── agent_workflow.py                      <- Workflow builder, tracked writes, tool registry
├── agent_review.py                        <- Flask Blueprint for agent UI + review bridge
├── agent_backends.py                      <- Pluggable backend abstraction + port manager
├── agent_mcp_server.py                    <- MCP server wrapping agent tools
├── git_ops.py                             <- Git clone/branch/push/deploy operations
├── templates/                             <- Jinja2 HTML templates
│   ├── base.html                          <- Base layout (Bootstrap 5 dark theme)
│   ├── ide_shell.html                     <- Full IDE shell (project switcher, chat, dashboard)
│   ├── _chat_panel.html                   <- Cascade-style agent chat panel partial
│   ├── _dashboard.html                    <- Agent dashboard sidebar partial
│   ├── setup.html                         <- Source/target configuration + session management
│   ├── browse.html                        <- Split-pane file browser
│   ├── inventory.html                     <- Full file inventory with filtering/sorting
│   ├── conflicts.html                     <- Conflict list overview
│   ├── conflict_detail.html               <- Single conflict with diff viewer
│   ├── merge_editor.html                  <- Interactive hunk-by-hunk merge editor
│   ├── coverage.html                      <- Per-source coverage stats
│   ├── scan_progress.html                 <- Scan progress (SSE)
│   ├── merge_progress.html                <- Merge progress (SSE)
│   ├── log.html                           <- Activity log
│   ├── _file_detail.html                  <- File detail partial (AJAX loaded)
│   ├── agent_repos.html                   <- Repo registration + model/deploy settings
│   ├── agent_sessions.html                <- Agent session dashboard
│   ├── agent_session_detail.html          <- Session detail + actions
│   ├── agent_conversation.html            <- Agent conversation viewer
│   └── agent_combined_diff.html           <- Multi-agent combined diff
├── agentic/                               <- Lightweight AI workflow engine
│   ├── engine/                            <- Runner, loop, OpenRouter client, OpenCode client
│   └── tools/                             <- Sandboxed filesystem tools
└── sessions/                              <- Runtime data (SQLite DBs, agent workspaces, cloned repos)
```

## Design Invariants (MUST NOT Violate)

1. **No silent file destruction**: Never delete or overwrite user files without explicit confirmation. The merge engine must skip files that already exist at the target with identical content, and all overwrites require user action.
2. **Session persistence**: Session data must always be persisted to SQLite, never held only in memory. Individual conflict resolutions use `save_item_resolution()` for instant writes; full inventory saves happen after scans via `save_inventory_state()`.

## Code Index (jcodemunch) — MANDATORY

This project has a jcodemunch code index at `.code-index/`. A jcodemunch MCP server is available that exposes code analysis tools.

**MANDATORY: NEVER use the Read tool, Grep tool, Glob tool, or Bash commands (grep, find, cat, head) to explore, search, or navigate code when jcodemunch MCP tools are available. The jcodemunch tools understand code structure (symbols, imports, dependencies, blast radius) — built-in tools only see raw text. Use Read/Grep only for non-code files (config, docs, logs) or when editing.**

### Key MCP tools to use

| Tool | When to use |
|------|-------------|
| `jcodemunch.search_symbols` | Finding where something is defined (instead of grep) |
| `jcodemunch.get_file_outline` | See all symbols in a file before reading it |
| `jcodemunch.get_symbol_source` | Read just one function/class (instead of reading the whole file) |
| `jcodemunch.get_blast_radius` | Before modifying a function — see what depends on it |
| `jcodemunch.get_dependency_graph` | Understand module-level import relationships |
| `jcodemunch.get_class_hierarchy` | Explore inheritance chains |
| `jcodemunch.search_text` | Full-text search across indexed files |
| `jcodemunch.get_file_tree` | Repository file structure |

### Workflow

1. **Before reading a file**: use `jcodemunch.get_file_outline` to see what's in it, then `jcodemunch.get_symbol_source` for specific symbols
2. **Finding definitions**: use `jcodemunch.search_symbols` instead of grep
3. **Understanding impact**: use `jcodemunch.get_blast_radius` before modifying shared functions
4. **Exploring structure**: use `jcodemunch.get_dependency_graph` and `jcodemunch.get_class_hierarchy`

### Symbol ID format

Symbol IDs follow `file_path::qualified_name#kind`, e.g.:
- `src/auth.py::login#function`
- `src/models.py::User#class`
- `src/models.py::User.save#method`

## General Coding Guidelines

- Python 3.8+ with type hints (uses `dataclasses`, `typing.Optional`, `pathlib.Path`)
- Core merge logic in `file_merger.py`; agent system in separate modules (`agent_*.py`)
- Use `dataclass` for data models (`FileVersion`, `MergeItem`, `SourceConfig`)
- Thread safety: SQLite connections are thread-local (`threading.local()`), use WAL mode
- Long path support on Windows (`\\?\` prefix for paths > 250 chars)
- SSE (Server-Sent Events) for real-time progress on scan/merge operations
- Only modify code within the scope of the current request
- Always check for existing functions before creating new ones

## Bug Fixing Guidelines

- Root-cause analysis before fixing
- Check if the bug violates a design invariant (especially file safety and session persistence)
- Fix the root cause, not the symptom
- Do NOT add defensive `try/except` blocks to suppress errors
- Do NOT skip failing operations — understand why they fail
- Do NOT add "guard clauses" that silently return early to avoid the bug path
- Keep the fix minimal — do not refactor surrounding code

## Critical Data Flows

### Agent Review Flow

```
1. User registers a repo (local path or git URL) and creates an agent session
2. Backend (OpenCode or builtin) is selected; OpenCode gets its own server per repo on a dynamic port
3. Agent checks out files, edits in workspace via tools (Python import or MCP)
4. Agent signals done — diff stats are computed for all modified files
5. User clicks "Review Changes" — review bridge creates MergeItem/FileVersion pairs
6. User reviews via existing merge UI (hunk-level accept/reject/edit)
7. Accepted changes are copied back to the original repo
8. For git repos: push to feature branch → optional SSH deploy to remote server
```

### File Recovery Flow

```
1. User configures sources and target directory via /setup
2. FileScanner.build_inventory() walks all sources, hashing files (threaded)
3. Items categorized: auto_unique / auto_identical / conflict
4. Inventory persisted to SQLite (save_inventory_state)
5. User reviews conflicts via /conflicts and /conflict/<path> (side-by-side diffs)
6. User resolves conflicts (select version or interactive merge)
7. MergeEngine.execute_merge() copies selected versions to target, skipping identical
```

## Environment Notes

- Developed on: Windows 11
- Shell syntax: PowerShell / bash (Git Bash)
- Package manager: pip
- Python dependencies: `flask` (+ `markupsafe`), `mcp` (optional, for MCP server), stdlib otherwise
- Run with: `python file_merger.py` → http://localhost:5000
