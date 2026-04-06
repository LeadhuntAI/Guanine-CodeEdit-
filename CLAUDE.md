# CLAUDE.md — Guanine (CodeEdit)

## Project Overview

**Guanine (CodeEdit)** — A multi-source file recovery and merge tool with a Flask web UI. Scans directories and editor local history (Windsurf, VS Code, Cursor), identifies conflicts between file versions across multiple sources, and lets you review side-by-side diffs and merge into a target directory. Includes a companion PowerShell script for standalone history extraction.

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
- **Editor History Extractor** (`file_merger.py` — `EditorHistoryExtractor` class) — Parses Windsurf/VS Code/Cursor local history directories (`entries.json`), resolves hashed backup files to original paths
- **Session Persistence** (`file_merger.py` — SQLite layer) — Stores scan results, merge items, file versions, and coverage stats in per-session SQLite databases with WAL mode
- **Flask Web UI** (`file_merger.py` — Flask routes + `templates/`) — Setup wizard, scan progress (SSE), inventory browser, conflict resolution, interactive merge editor, coverage dashboard
- **PowerShell Recovery Script** (`restore_deleted_files.ps1`) — Standalone script for finding and restoring files from editor history without the web UI
- **HTML Templates** (`templates/`) — 14 Jinja2 templates: setup, browse, inventory, conflicts, conflict detail, merge editor, coverage, extraction, progress pages

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
├── file_merger.py                         <- Main application (~2,860 lines, monolithic)
├── templates/                             <- Jinja2 HTML templates (14 files)
│   ├── base.html                          <- Base layout (Bootstrap 5 dark theme)
│   ├── setup.html                         <- Source/target configuration + session management
│   ├── browse.html                        <- Split-pane file browser
│   ├── inventory.html                     <- Full file inventory with filtering/sorting
│   ├── conflicts.html                     <- Conflict list overview
│   ├── conflict_detail.html               <- Single conflict with diff viewer
│   ├── merge_editor.html                  <- Interactive hunk-by-hunk merge editor
│   ├── coverage.html                      <- Per-source coverage stats
│   ├── extract_history.html               <- Editor history extraction setup
│   ├── extract_progress.html              <- Extraction progress (SSE)
│   ├── scan_progress.html                 <- Scan progress (SSE)
│   ├── merge_progress.html                <- Merge progress (SSE)
│   ├── log.html                           <- Activity log + recovery suggestions
│   └── _file_detail.html                  <- File detail partial (AJAX loaded)
├── restore_deleted_files.ps1              <- PowerShell companion script
├── commands.txt                           <- Session resume notes
└── sessions/                              <- Runtime data (SQLite DBs per session)
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

### Editor History Extraction Flow (touches: `EditorHistoryExtractor` → filesystem)

```
1. Extractor scans known editor history paths (Windsurf, VS Code, Cursor)
2. Parses entries.json in each hashed subdirectory to recover original file paths
3. Resolves backup files via 3 methods: entry.id, entry.source URI, newest file
4. Filters by project folder and time window
5. Copies files to destination preserving original directory structure
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
- **Editor history paths** — hardcoded in `EditorHistoryExtractor.EDITOR_HISTORY_PATHS` for Windows (Windsurf, VS Code, Cursor)
- **Ignore patterns** — `DEFAULT_IGNORE` set in source (`.git`, `__pycache__`, `node_modules`, etc.)

## General Coding Guidelines

- Python 3.8+ with type hints (uses `dataclasses`, `typing.Optional`, `pathlib.Path`)
- Single-file architecture — all Python code lives in `file_merger.py`
- Use `dataclass` for data models (`FileVersion`, `MergeItem`, `SourceConfig`)
- Thread safety: SQLite connections are thread-local (`threading.local()`), use WAL mode
- Long path support on Windows (`\\?\` prefix for paths > 250 chars)
- SSE (Server-Sent Events) for real-time progress on scan/merge/extraction operations
- Only modify code within the scope of the current request
- Always check for existing functions before creating new ones

## Bug Fixing Guidelines

When working on any bug fix, read `.claude/rules/bug-fixing.md` first.
Key points:
- Root-cause analysis before fixing
- Check if the bug violates a design invariant (especially file safety and session persistence)
- Test with multiple source directories and editor history formats
- Verify SQLite persistence survives app restart

## Environment Notes

- Developed on: Windows 11
- Shell syntax: PowerShell
- Package manager: pip
- Python dependencies: `flask` (+ `markupsafe`), stdlib only otherwise
- Run with: `python file_merger.py` → http://localhost:5000
