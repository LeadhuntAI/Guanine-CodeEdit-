# CLAUDE.md — Guanine (CodeEdit)

## Project Overview

**Guanine (CodeEdit)** — A sandboxed coding agent review system and multi-source file merge tool with a Flask web UI. AI coding agents edit file copies in isolated workspaces; humans review changes through side-by-side diffs and hunk-level merge editing. Also supports general-purpose multi-source directory scanning, conflict detection, and merging.

Stack: Python, Flask, SQLite, Jinja2, Bootstrap 5, pytest

## Rule and Skill Discovery Protocol

Do NOT expect a full list of rules or skills in this file.
The project uses a decentralised documentation system. Follow this protocol:

### Before starting any task:

1. **Identify affected modules.** Look at the file paths involved in the task.
   Map each path to its module or app.

2. **Read the module's index.** For each affected module, read:
   ```
   <module-path>/.claude/RULES_INDEX.md
   ```
   This lists all documentation rules, coding rules, and skills
   available for that module with one-line summaries.

3. **Load relevant rules only.** Based on the index and the specific
   files you are editing, read only the rules that apply.
   Do NOT load rules for unrelated modules or files.

4. **Check global rules.** Always check these locations for project-wide
   rules that apply regardless of module:
   ```
   .claude/RULES_INDEX.md           (global rules index)
   .claude/rules/                   (global coding rules)
   .claude/rules/invariants.md      (design invariants — MUST NOT be violated)
   ```

### For cross-module tasks:

If a task spans multiple modules, read the RULES_INDEX.md for ALL affected
modules and load rules from each that are relevant.

### After completing code changes:

If you created or significantly modified a file, check if a documentation
rule exists for it. If one exists and may be stale, ask: "Should I update
the documentation rule for this file?" If the user agrees, use the
`code-documenter` skill.

## Architecture

- **File Scanner** (`file_merger.py` — `FileScanner` class) — Walks source directories, computes SHA-256 hashes, detects binary files, categorizes items as unique/identical/conflict
- **Merge Engine** (`file_merger.py` — `MergeEngine` class) — Generates unified and side-by-side diffs, executes file copy/merge operations with skip-if-identical logic
- **Session Persistence** (`file_merger.py` — SQLite layer) — Stores scan results, merge items, file versions, and coverage stats in per-session SQLite databases with WAL mode
- **Flask Web UI** (`file_merger.py` — Flask routes + `templates/`) — Setup wizard, scan progress (SSE), inventory browser, conflict resolution, interactive merge editor, coverage dashboard
- **Agent Review System** (`agent_schema.py`, `agent_tools.py`, `agent_workflow.py`, `agent_review.py`) — Sandboxed agent workspaces, tool exposure via Python/MCP, review bridge to merge UI
- **MCP Server** (`agent_mcp_server.py`) — Model Context Protocol server for external agent integration
- **HTML Templates** (`templates/`) — Jinja2 templates for merge UI and agent review pages

## Directory Layout

```
Guanine(CodeEdit)/
├── CLAUDE.md                              <- You are here
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
├── agent_mcp_server.py                    <- MCP server wrapping agent tools
├── templates/                             <- Jinja2 HTML templates
│   ├── base.html                          <- Base layout (Bootstrap 5 dark theme)
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
│   ├── agent_repos.html                   <- Repo registration
│   ├── agent_sessions.html                <- Agent session dashboard
│   ├── agent_session_detail.html          <- Session detail + actions
│   ├── agent_conversation.html            <- Agent conversation viewer
│   └── agent_combined_diff.html           <- Multi-agent combined diff
├── agentic/                               <- Lightweight AI workflow engine
│   ├── engine/                            <- Runner, loop, OpenRouter client, knowledge
│   └── tools/                             <- Sandboxed filesystem tools
└── sessions/                              <- Runtime data (SQLite DBs per session + agent workspaces)
```

## Key Conventions

- Documentation rules are stored in `.claude/rules/docs/` — either at
  the project root (cross-module) or inside a module's own `.claude/rules/docs/`.
- Coding convention rules are stored in `.claude/rules/` (no `docs/` subfolder).
- Skills are stored in `.claude/skills/<skill-name>/SKILL.md`.

## Design Invariants (MUST NOT Violate)

Read `.claude/rules/invariants.md` for the full list with file references.

1. **No silent file destruction**: Never delete or overwrite user files without explicit confirmation. The merge engine must skip files that already exist at the target with identical content, and all overwrites require user action.
2. **Session persistence**: Session data must always be persisted to SQLite, never held only in memory. Individual conflict resolutions use `save_item_resolution()` for instant writes; full inventory saves happen after scans via `save_inventory_state()`.

## Critical Data Flows

### File Recovery Flow (touches: `FileScanner` → `MergeEngine` → SQLite → Web UI)

```
1. User configures sources and target directory via /setup
2. FileScanner.build_inventory() walks all sources, hashing files (threaded)
3. Items categorized: auto_unique / auto_identical / conflict
4. Inventory persisted to SQLite (save_inventory_state)
5. User reviews conflicts via /conflicts and /conflict/<path> (side-by-side diffs)
6. User resolves conflicts (select version or interactive merge)
7. MergeEngine.execute_merge() copies selected versions to target, skipping identical
```

### Agent Review Flow (touches: `agent_schema.py` → `agent_tools.py` → `agent_review.py` → merge UI)

```
1. User registers a repo and creates an agent session (workspace is provisioned)
2. Agent checks out files, edits in workspace via tools (Python import or MCP)
3. Agent signals done — diff stats are computed for all modified files
4. User clicks "Review Changes" — review bridge creates MergeItem/FileVersion pairs
5. User reviews via existing merge UI (hunk-level accept/reject/edit)
6. Accepted changes are copied back to the original repo
```

## Testing Protocol

When writing or executing tests, **read `.claude/rules/testing.md` first**. Key points:

1. **Test plans** (markdown) go in `.claude/tests/<feature>-test-plan.md`.
   Archive completed plans to `.claude/tests/history/`.
2. **Test code** goes in `tests/`.
3. No test framework is currently configured — use `pytest` for any new tests.
4. Run tests: `pytest tests/ -v`

## Configuration

- **No `.env` file** — the app uses hardcoded defaults (port 5000, Flask secret key in source)
- **Session storage** — `sessions/` directory alongside `file_merger.py`, each session gets its own SQLite DB
- **Ignore patterns** — `DEFAULT_IGNORE` set in source (`.git`, `__pycache__`, `node_modules`, etc.)

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

When working on any bug fix, read `.claude/rules/bug-fixing.md` first.
Key points:
- Root-cause analysis before fixing
- Check if the bug violates a design invariant (especially file safety and session persistence)
- Test with multiple source directories
- Verify SQLite persistence survives app restart

## Sandboxed Editing Workflow

A PreToolUse hook enforces sandboxed editing when `.claude/sandbox-active` exists. When active, ALL Edit/Write operations on project files are blocked unless the file is under `sessions/` (agent workspace).

### How to make changes (when sandbox is ON)

1. **Create a session** (or reuse the current one):
   ```
   mcp__guanine__create_session(repo_id="<REPO_ID>", task_description="what you're doing")
   ```
   Returns `session_id` and `workspace_path`. **Remember the workspace_path.**

2. **Checkout files** you need to edit:
   ```
   mcp__guanine__checkout_file(path="relative/path/to/file.py")
   ```
   Returns `workspace_file_path` — the absolute path to use with Edit/Write.

3. **Edit files in the workspace** using native Edit/Write tools with absolute workspace paths.

4. **Read repo files** for context — use Read tool directly on any project file. No checkout needed for reading.

5. **Create new files** — Write them into the workspace at the appropriate relative path. Detected automatically when you signal done.

6. **Signal completion**:
   ```
   mcp__guanine__signal_done(summary="What I changed and why")
   ```

7. User reviews changes at http://localhost:5000/agent/sessions

### Toggle Commands

The sandbox is controlled by the `.claude/sandbox-active` flag file:

- **"turn off sandbox"** / **"sandbox off"**: Run `rm .claude/sandbox-active`. All subsequent edits go direct.
- **"turn sandbox on"** / **"sandbox on"**: Run `touch .claude/sandbox-active`. Edits are sandboxed again.
- **"edit this directly"**: Run `rm .claude/sandbox-active`, make the edit, then `touch .claude/sandbox-active`.

### Important Details

- **Repo ID**: Pre-registered. Call `mcp__guanine__list_repos()` to find it.
- **One session per task**: Create a new session for each distinct task. Reuse if continuing the same task.
- **After compaction**: The SessionStart hook re-injects workspace path and sandbox status. Call `mcp__guanine__get_workspace_info()` if needed.
- **Bash is unrestricted**: You can run git, python, pytest, etc. Do NOT use Bash to write files outside `sessions/` when sandbox is active.

## Environment Notes

- Developed on: Windows 11
- Shell syntax: PowerShell / bash (Git Bash)
- Package manager: pip
- Python dependencies: `flask` (+ `markupsafe`), `mcp` (optional, for MCP server), stdlib otherwise
- Run with: `python file_merger.py` → http://localhost:5000
