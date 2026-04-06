"""
MCP Server for the Guanine sandboxed coding agent review system.

Exposes agent tools via the Model Context Protocol so external agents
(Claude Desktop, Cursor, other MCP clients) can use the sandbox system.

Usage:
    # stdio transport (default for Claude Desktop / local agents)
    python agent_mcp_server.py

    # Or with streamable HTTP transport
    python agent_mcp_server.py --transport streamable-http --port 8080

Configure in your MCP client:
    {
        "mcpServers": {
            "guanine": {
                "command": "python",
                "args": ["C:/path/to/agent_mcp_server.py"],
                "env": {}
            }
        }
    }
"""

from __future__ import annotations

import json
import os
import sys

from mcp.server.fastmcp import FastMCP

import agent_schema
import agent_tools

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="guanine-codeedit",
    instructions=(
        "Guanine CodeEdit agent sandbox. Agents work on file copies in isolated "
        "workspaces. Use checkout_file to get files, edit them, then signal_done "
        "when finished. A human reviews and merges changes back to the repo."
    ),
)

# ---------------------------------------------------------------------------
# Session state — bound when a session is activated
# ---------------------------------------------------------------------------

_active_session: dict | None = None
_active_repo: dict | None = None


def _require_session() -> tuple[dict, dict]:
    """Return (session, repo) or raise."""
    if _active_session is None or _active_repo is None:
        raise ValueError(
            "No active session. Call activate_session or create_session first."
        )
    return _active_session, _active_repo


def _auto_resume():
    """Auto-activate the most recent running session on startup."""
    global _active_session, _active_repo
    try:
        sessions = agent_schema.list_sessions(status='running')
        if sessions:
            s = sessions[0]
            _active_session = agent_schema.get_session(s['session_id'])
            if _active_session:
                _active_repo = agent_schema.get_repo(_active_session['repo_id'])
    except Exception:
        pass

_auto_resume()


# ---------------------------------------------------------------------------
# Session management tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_workspace_info() -> str:
    """Get the current session's workspace path and status.
    Use this after context compaction to recover the workspace path."""
    session, repo = _require_session()
    return json.dumps({
        "session_id": session["session_id"],
        "workspace_path": session["workspace_path"],
        "repo_id": repo["repo_id"],
        "repo_path": repo["repo_path"],
        "status": session["status"],
        "task_description": session["task_description"],
    }, indent=2)


@mcp.tool()
def list_repos() -> str:
    """List all registered repositories."""
    repos = agent_schema.list_repos()
    return json.dumps([{
        "repo_id": r["repo_id"],
        "repo_name": r["repo_name"],
        "repo_path": r["repo_path"],
        "file_count": r["file_count"],
    } for r in repos], indent=2)


@mcp.tool()
def list_sessions(repo_id: str = "", status: str = "") -> str:
    """List agent sessions, optionally filtered by repo_id and/or status."""
    sessions = agent_schema.list_sessions(
        repo_id=repo_id or None,
        status=status or None,
    )
    return json.dumps([{
        "session_id": s["session_id"],
        "repo_id": s["repo_id"],
        "task_description": s["task_description"],
        "status": s["status"],
        "created_at": s["created_at"],
    } for s in sessions], indent=2)


@mcp.tool()
def create_session(repo_id: str, task_description: str,
                   agent_model: str = "", external_context: str = "") -> str:
    """Create a new agent session for a registered repo.
    Returns session details including session_id and workspace_path."""
    session = agent_schema.create_session(
        repo_id=repo_id,
        task_description=task_description,
        agent_model=agent_model or None,
        external_context=external_context or None,
    )
    # Auto-activate the newly created session
    global _active_session, _active_repo
    _active_session = session
    _active_repo = agent_schema.get_repo(repo_id)
    agent_schema.update_session_status(session["session_id"], "running")
    _active_session["status"] = "running"

    return json.dumps({
        "session_id": session["session_id"],
        "workspace_path": session["workspace_path"],
        "status": "running",
        "message": "Session created and activated. Use checkout_file to get files.",
    }, indent=2)


@mcp.tool()
def activate_session(session_id: str) -> str:
    """Activate an existing session to work on it.
    Must be called before using file tools if you didn't just create the session."""
    global _active_session, _active_repo
    session = agent_schema.get_session(session_id)
    if session is None:
        return json.dumps({"error": f"Session not found: {session_id}"})

    repo = agent_schema.get_repo(session["repo_id"])
    if repo is None:
        return json.dumps({"error": "Associated repo not found"})

    _active_session = session
    _active_repo = repo

    if session["status"] == "pending":
        agent_schema.update_session_status(session_id, "running")
        _active_session["status"] = "running"

    return json.dumps({
        "session_id": session_id,
        "status": _active_session["status"],
        "workspace_path": session["workspace_path"],
        "task_description": session["task_description"],
    }, indent=2)


# ---------------------------------------------------------------------------
# File tools — require an active session
# ---------------------------------------------------------------------------

@mcp.tool()
def checkout_file(path: str) -> str:
    """Copy a file from the repo to your workspace so you can edit it.
    You must checkout a file before you can read or edit it."""
    session, repo = _require_session()
    return agent_tools.checkout_file(
        path=path,
        session_id=session["session_id"],
        repo_path=repo["repo_path"],
        workspace_path=session["workspace_path"],
    )


@mcp.tool()
def checkout_files(paths: list[str]) -> str:
    """Batch checkout multiple files from the repo to your workspace."""
    session, repo = _require_session()
    return agent_tools.checkout_files(
        paths=paths,
        session_id=session["session_id"],
        repo_path=repo["repo_path"],
        workspace_path=session["workspace_path"],
    )


@mcp.tool()
def list_repo_files(pattern: str = "*") -> str:
    """List files in the original repo matching a glob pattern.
    Read-only — does not checkout files. Use to discover what's available."""
    _, repo = _require_session()
    return agent_tools.list_repo_files(
        pattern=pattern,
        repo_path=repo["repo_path"],
    )


@mcp.tool()
def get_repo_file_content(path: str, start_line: int = 0,
                          end_line: int = 0) -> str:
    """Read a file from the repo WITHOUT checking it out.
    For context-gathering. Use start_line/end_line for ranges (1-based)."""
    _, repo = _require_session()
    return agent_tools.get_repo_file_content(
        path=path,
        repo_path=repo["repo_path"],
        start_line=start_line or None,
        end_line=end_line or None,
    )


@mcp.tool()
def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read a file from your workspace. Must be checked out first."""
    session, _ = _require_session()
    from agentic.tools import read_file as rf
    return rf.execute(
        path=path,
        start_line=start_line or None,
        end_line=end_line or None,
        _base_dir=session["workspace_path"],
    )


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write content to a file in your workspace.
    Changes are tracked automatically for later review."""
    session, repo = _require_session()
    import agent_workflow
    return agent_workflow.tracked_write_file(
        path=path,
        content=content,
        _base_dir=session["workspace_path"],
        session_id=session["session_id"],
        repo_path=repo["repo_path"],
    )


@mcp.tool()
def search_code(pattern: str, file_pattern: str = "",
                path: str = ".") -> str:
    """Search for a regex pattern in your workspace files."""
    session, _ = _require_session()
    from agentic.tools import search_code as sc
    return sc.execute(
        pattern=pattern,
        file_pattern=file_pattern or None,
        path=path,
        _base_dir=session["workspace_path"],
    )


@mcp.tool()
def list_directory(path: str = ".", recursive: bool = False) -> str:
    """List files and subdirectories in your workspace."""
    session, _ = _require_session()
    from agentic.tools import list_directory as ld
    return ld.execute(
        path=path,
        recursive=recursive,
        _base_dir=session["workspace_path"],
    )


@mcp.tool()
def get_file_tree(max_depth: int = 4) -> str:
    """Get the file tree of your workspace."""
    session, _ = _require_session()
    from agentic.tools import get_file_tree as ft
    return ft.execute(
        max_depth=max_depth,
        _base_dir=session["workspace_path"],
    )


@mcp.tool()
def run_command(command: str, timeout: int = 120) -> str:
    """Run a shell command in your workspace.
    Only permitted commands are allowed unless the repo allows free execution."""
    session, _ = _require_session()
    return agent_tools.run_command(
        command=command,
        session_id=session["session_id"],
        workspace_path=session["workspace_path"],
        timeout=timeout,
    )


@mcp.tool()
def signal_done(summary: str) -> str:
    """Signal that you have completed your task.
    Provide a summary of what you changed. This finalizes the session for human review."""
    session, _ = _require_session()
    return agent_tools.signal_done(
        summary=summary,
        session_id=session["session_id"],
    )


# ---------------------------------------------------------------------------
# Resources — session status and file list
# ---------------------------------------------------------------------------

@mcp.resource("session://{session_id}/status")
def session_status(session_id: str) -> str:
    """Get the current status of an agent session."""
    session = agent_schema.get_session(session_id)
    if session is None:
        return json.dumps({"error": "Session not found"})
    return json.dumps({
        "session_id": session["session_id"],
        "status": session["status"],
        "task_description": session["task_description"],
        "file_stats": session.get("file_stats", {}),
        "created_at": session["created_at"],
        "completed_at": session["completed_at"],
    }, indent=2)


@mcp.resource("session://{session_id}/files")
def session_files(session_id: str) -> str:
    """List files in an agent session with diff stats."""
    files = agent_schema.get_session_files(session_id)
    return json.dumps(files, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Guanine MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"],
                        default="stdio")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.transport == "streamable-http":
        mcp.run(transport="streamable-http", host="127.0.0.1", port=args.port)
    else:
        mcp.run(transport="stdio")
