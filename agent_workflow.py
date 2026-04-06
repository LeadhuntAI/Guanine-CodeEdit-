"""
Workflow builder and tracked tool wrappers for agent sessions.

Builds tool registries that bind existing agentic/tools/ to a specific
workspace, wraps write_file with diff-stat tracking, and generates
workflow definitions for the agentic engine.
"""

from __future__ import annotations

import json
import os
from functools import partial
from typing import Optional

import agent_schema
import agent_tools
from agentic.tools import (
    read_file,
    list_directory,
    search_code,
    get_file_tree,
    write_file,
)


# ---------------------------------------------------------------------------
# Tracked write_file — wraps the original to update session_files stats
# ---------------------------------------------------------------------------

def tracked_write_file(path: str, content: str, _base_dir: str,
                       session_id: str, repo_path: str, **kwargs) -> str:
    """Write file in workspace AND update session_files with diff stats."""
    # Perform the actual write
    result_json = write_file.execute(path=path, content=content, _base_dir=_base_dir)
    result = json.loads(result_json)

    if result.get('error'):
        return result_json

    # Compute new hash
    new_hash = agent_tools._compute_hash_str(content)

    # Check if this file was checked out or is new
    files = agent_schema.get_session_files(session_id)
    existing = [f for f in files if f['relative_path'] == path.replace('\\', '/')]

    norm_path = path.replace('\\', '/')

    if existing:
        # File was checked out — compute diff against original
        repo_file = os.path.join(repo_path, norm_path)
        if os.path.isfile(repo_file):
            added, removed = agent_tools._compute_diff_stats(repo_file, content)
        else:
            added = content.count('\n') + (1 if content and not content.endswith('\n') else 0)
            removed = 0

        agent_schema.update_file_stats(
            session_id, norm_path,
            current_hash=new_hash,
            lines_added=added,
            lines_removed=removed,
            status='modified'
        )
    else:
        # New file created by agent
        line_count = content.count('\n') + (1 if content and not content.endswith('\n') else 0)
        agent_schema.record_new_file(session_id, norm_path, new_hash, line_count)

    return result_json


# ---------------------------------------------------------------------------
# Tool registry builder
# ---------------------------------------------------------------------------

def build_tool_registry(session_id: str, workspace_path: str,
                        repo_path: str) -> dict:
    """Build a tool registry with all tools bound to a specific session/workspace.

    Returns dict of {tool_name: callable} ready for use with
    agentic/engine/tool_executor.py's execute_tool_call().
    """
    ws = workspace_path

    return {
        # Existing tools from agentic/tools/ — sandboxed to workspace
        'read_file': partial(read_file.execute, _base_dir=ws),
        'write_file': partial(tracked_write_file,
                              _base_dir=ws,
                              session_id=session_id,
                              repo_path=repo_path),
        'list_directory': partial(list_directory.execute, _base_dir=ws),
        'search_code': partial(search_code.execute, _base_dir=ws),
        'get_file_tree': partial(get_file_tree.execute, _base_dir=ws),

        # Agent-specific tools
        'checkout_file': partial(agent_tools.checkout_file,
                                 session_id=session_id,
                                 repo_path=repo_path,
                                 workspace_path=ws),
        'checkout_files': partial(agent_tools.checkout_files,
                                  session_id=session_id,
                                  repo_path=repo_path,
                                  workspace_path=ws),
        'list_repo_files': partial(agent_tools.list_repo_files,
                                   repo_path=repo_path),
        'get_repo_file_content': partial(agent_tools.get_repo_file_content,
                                         repo_path=repo_path),
        'signal_done': partial(agent_tools.signal_done,
                               session_id=session_id),
        'run_command': partial(agent_tools.run_command,
                               session_id=session_id,
                               workspace_path=ws),
    }


# ---------------------------------------------------------------------------
# Tool definitions for the agentic engine (OpenAI-format schemas)
# ---------------------------------------------------------------------------

AGENT_TOOL_DEFINITIONS = [
    {
        "name": "checkout_file",
        "description": "Copy a file from the original repo to your workspace so you can edit it. You must checkout a file before you can read or edit it in your workspace.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "checkout_files",
        "description": "Batch checkout multiple files from the repo to your workspace.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths relative to repo root"
                }
            },
            "required": ["paths"]
        }
    },
    {
        "name": "list_repo_files",
        "description": "List files in the original repo matching a glob pattern. Read-only, does not checkout files. Use this to discover what files exist before checking them out.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (default: '*')"}
            }
        }
    },
    {
        "name": "get_repo_file_content",
        "description": "Read a file from the original repo WITHOUT checking it out. Use for context-gathering when you don't need to edit the file.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"},
                "start_line": {"type": "integer", "description": "Start line (1-based)"},
                "end_line": {"type": "integer", "description": "End line (inclusive)"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "read_file",
        "description": "Read a file from your workspace. The file must have been checked out first.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace root"},
                "start_line": {"type": "integer", "description": "Start line (1-based)"},
                "end_line": {"type": "integer", "description": "End line (inclusive)"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write content to a file in your workspace. If the file was checked out, changes are tracked. If the file is new, it's recorded as a new file.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace root"},
                "content": {"type": "string", "description": "Full file content to write"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories in your workspace.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to workspace (default: '.')"},
                "recursive": {"type": "boolean", "description": "List recursively (default: false)"}
            }
        }
    },
    {
        "name": "search_code",
        "description": "Search for a regex pattern in workspace files.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "file_pattern": {"type": "string", "description": "Glob filter (e.g. '*.py')"},
                "path": {"type": "string", "description": "Directory to search in"}
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "get_file_tree",
        "description": "Get the file tree of your workspace.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "max_depth": {"type": "integer", "description": "Max depth (default: 4)"}
            }
        }
    },
    {
        "name": "signal_done",
        "description": "Signal that you have completed your task. Provide a summary of what you did. This will finalize the session for human review.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Summary of changes made and task completion status"}
            },
            "required": ["summary"]
        }
    },
    {
        "name": "run_command",
        "description": "Run a shell command in your workspace (e.g. to run tests). Only permitted commands are allowed unless the repo has free command execution enabled.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 120)"}
            },
            "required": ["command"]
        }
    }
]


# ---------------------------------------------------------------------------
# Workflow definition builder
# ---------------------------------------------------------------------------

def build_workflow(task_description: str,
                   model: str = "openai/gpt-4o-mini",
                   system_prompt: Optional[str] = None,
                   max_iterations: int = 50) -> dict:
    """Build a workflow JSON definition for an agent coding session.

    Returns a dict suitable for agentic/engine/runner.py's run_workflow().
    """
    if system_prompt is None:
        system_prompt = _default_system_prompt()

    return {
        "name": "agent_coding_session",
        "description": f"Agent coding task: {task_description[:100]}",
        "layers": [
            {
                "name": "coding_task",
                "layer_type": "agentic_loop",
                "loop_mode": "native",
                "model": model,
                "system_message": system_prompt,
                "content": task_description,
                "max_iterations": max_iterations,
                "tools": [t["name"] for t in AGENT_TOOL_DEFINITIONS],
            }
        ]
    }


def _default_system_prompt() -> str:
    return """You are a coding agent working in a sandboxed workspace. You have access to tools that let you:

1. **Browse the repo**: Use `list_repo_files` and `get_repo_file_content` to explore the original codebase (read-only).
2. **Checkout files**: Use `checkout_file` or `checkout_files` to copy files from the repo to your workspace for editing.
3. **Edit files**: Use `read_file` and `write_file` to read/modify files in your workspace.
4. **Search code**: Use `search_code` to find patterns in your workspace files.
5. **Run commands**: Use `run_command` to execute tests or other permitted commands.
6. **Signal completion**: Use `signal_done` when your task is finished.

## Workflow

1. First, explore the repo to understand the codebase structure and find relevant files.
2. Checkout the files you need to modify.
3. Make your changes using write_file.
4. If tests are available, run them to verify your changes.
5. Call signal_done with a summary of what you changed and why.

## Rules

- Only edit files in your workspace (checked-out copies). You cannot modify the original repo directly.
- Checkout files before trying to read or edit them in your workspace.
- Keep changes minimal and focused on the task.
- Write clear, well-structured code.
- If you encounter errors, investigate and fix them rather than giving up.
"""
