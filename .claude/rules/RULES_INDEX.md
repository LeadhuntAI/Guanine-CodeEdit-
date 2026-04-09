# Rules Index

Auto-generated index of project rules and documentation.

## Documentation Rules

### Agent Core System
- [docs/agent-core-schema.md](docs/agent-core-schema.md) — SQLite-backed persistence for repos, sessions, files, conversations, and review decisions. Most-imported module (5 importers).
- [docs/agent-tools-and-workflow.md](docs/agent-tools-and-workflow.md) — Sandboxed file operations, shell execution, tool registry, and workflow orchestration.
- [docs/agent-mcp-server.md](docs/agent-mcp-server.md) — MCP server exposing agent capabilities to external clients (Claude Code, Cursor, etc.).
- [docs/agent-review.md](docs/agent-review.md) — Flask Blueprint: agent UI, review bridge, chat SSE, git push/deploy, model management, OpenCode install/status.

### Agent Backends & Remote
- [docs/agent-backends.md](docs/agent-backends.md) — Pluggable backend abstraction (BuiltinBackend, OpenCodeBackend), per-repo dynamic port allocation, backend factory.
- [docs/opencode-client.md](docs/opencode-client.md) — OpenCode HTTP client: health checks, auto-start, session/message management, SSE streaming.
- [docs/git-ops.md](docs/git-ops.md) — Git operations: clone, pull, branch, commit, push, SSH deploy for remote project workflows.

### Engine & Tools
- [docs/agentic-engine.md](docs/agentic-engine.md) — Lightweight autonomous AI workflow engine with multi-layer orchestration, dual-mode agentic loops (ReAct/native), OpenRouter HTTP client, safe tool execution, and knowledge resolution.
- [docs/agentic-tools.md](docs/agentic-tools.md) — Sandboxed filesystem and code search toolkit — 5 tools (read_file, write_file, list_directory, search_code, get_file_tree) with path validation and JSON interface.

### Web UI
- [docs/ui-templates-agent.md](docs/ui-templates-agent.md) — Agent session, conversation, diff, repo, and IDE shell templates.
- [docs/ui-templates-merge-and-browse.md](docs/ui-templates-merge-and-browse.md) — File browsing, conflict resolution, merge progress templates.

### Application Core
- [docs/file-merger-and-sandbox.md](docs/file-merger-and-sandbox.md) — Core Flask app: multi-source file recovery, conflict detection, merge editing, SQLite persistence.
- [docs/project-overview.md](docs/project-overview.md) — High-level project overview, feature index, and area map.
