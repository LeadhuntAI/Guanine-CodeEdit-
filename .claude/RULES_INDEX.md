# Global Rules Index

## Coding Rules
- [invariants.md](rules/invariants.md) — Design invariants that MUST NOT be violated in any code change
- [testing.md](rules/testing.md) — Decentralized test structure, mock policy, invariant compliance tests
- [bug-fixing.md](rules/bug-fixing.md) — Root-cause discipline, invariant checks, pre-commit checklist

## Documentation Rules
- [rules/docs/](rules/docs/) — Cross-module documentation rules (created on demand via code-documenter skill)
- [rules/docs/agentic-tools.md](rules/docs/agentic-tools.md) — Provides sandboxed file-system operations for the agentic engine. Each tool module exposes a uniform `execute(**kwargs, _base_dir) -> str` interface t
- [rules/docs/agentic-engine.md](rules/docs/agentic-engine.md) — Standalone workflow execution engine for LLM-powered agents. Workflows consist of multiple processing layers (prompts, agentic loops, templates, tool 
- [rules/docs/agent-mcp-server.md](rules/docs/agent-mcp-server.md) — MCP protocol adapter that exposes the Guanine agent sandbox to external MCP clients (Claude Desktop, Cursor). It wraps session management, file operat
- [rules/docs/agent-session-schema.md](rules/docs/agent-session-schema.md) — Core data layer providing SQLite-backed persistence for the agent system. Manages repos, sessions, file checkouts, conversation history, and review de
- [rules/docs/agent-sandbox-tools.md](rules/docs/agent-sandbox-tools.md) — Provides the core sandboxed execution environment for AI coding agents. Agents checkout files from a repository into an isolated workspace, make modif
- [rules/docs/ide-templates-and-static.md](rules/docs/ide-templates-and-static.md) — Contains all Jinja2 HTML templates and static assets for the Guanine application. Provides two distinct UI surfaces: (1) a **core recovery workflow**
- [rules/docs/web-server-and-merge-engine.md](rules/docs/web-server-and-merge-engine.md) — Flask-based web IDE for multi-source file merging. Provides file scanning and inventory building across multiple source directories, conflict detectio
- [rules/docs/agent-review-and-git.md](rules/docs/agent-review-and-git.md) — Flask Blueprint controller and supporting services for the agent code review system. Provides hunk-level code review where multiple agents modify file      

## Skills
- [code-documenter](skills/code-documenter/SKILL.md) — Analyse a module and generate/update its documentation rule

## Project Overview



- [docs/project-overview.md](docs/project-overview.md) — High-level project description, feature index, and architecture overview linking to all area docs