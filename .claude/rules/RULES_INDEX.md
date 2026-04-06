# Rules Index

Auto-generated index of project rules and documentation.

## Documentation Rules

- [docs/project-root-and-scripts.md](docs/project-root-and-scripts.md) — Project entry point (CLAUDE.md), standalone PowerShell recovery script, sessions/ runtime directory, and operational metadata.
- [docs/web-templates.md](docs/web-templates.md) — Jinja2 template suite for the Flask File Recovery Merger: base layout, setup wizard, file browser, conflict resolution, merge editor, coverage dashboard, progress pages (SSE), and activity log.
- [docs/agentic-docs-and-workflows.md](docs/agentic-docs-and-workflows.md) — Documentation suite, templates, and example definitions for the agentic workflow engine — covers workflows, skills, knowledge system, JSON definitions, and execution model.
- [docs/core-application.md](docs/core-application.md) — Monolithic Flask application (file_merger.py) implementing multi-source file recovery, conflict detection, interactive merge editing, SQLite persistence, and SSE progress streaming.
- [docs/agentic-engine.md](docs/agentic-engine.md) — Lightweight autonomous AI workflow engine with multi-layer orchestration, dual-mode agentic loops (ReAct/native), OpenRouter HTTP client, safe tool execution, and knowledge resolution for rules/skills injection into LLM prompts.
- [docs/agentic-tools.md](docs/agentic-tools.md) — Sandboxed filesystem and code search toolkit for the agentic engine — 5 tools (read_file, write_file, list_directory, search_code, get_file_tree) with path validation, JSON interface, and declarative registry via definitions.json.
