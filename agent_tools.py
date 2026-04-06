"""
Agent tool functions for the sandboxed coding agent review system.

These are the single source of truth for agent capabilities.
Co-located agents import them directly; remote agents access them via MCP.
Every function returns a JSON string (same contract as agentic/tools/).
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
from typing import Optional

import agent_schema


# ---------------------------------------------------------------------------
# Path validation (shared with agentic/tools/)
# ---------------------------------------------------------------------------

def _validate_path(path: str, base_dir: str) -> str | None:
    """Resolve path and ensure it stays within base_dir."""
    resolved = os.path.realpath(os.path.join(base_dir, path))
    base = os.path.realpath(base_dir)
    if not resolved.startswith(base + os.sep) and resolved != base:
        return None
    return resolved


def _compute_hash(filepath: str) -> str:
    """SHA-256 hash of a file."""
    h = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except (OSError, PermissionError):
        return "ERROR"
    return h.hexdigest()


def _compute_hash_str(content: str) -> str:
    """SHA-256 hash of a string."""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _compute_diff_stats(original_path: str, new_content: str) -> tuple[int, int]:
    """Compute lines added/removed between original file and new content."""
    try:
        with open(original_path, 'r', encoding='utf-8', errors='replace') as f:
            original_lines = f.readlines()
    except (OSError, PermissionError):
        original_lines = []

    new_lines = new_content.splitlines(keepends=True)
    diff = list(difflib.unified_diff(original_lines, new_lines, n=0))

    added = 0
    removed = 0
    for line in diff:
        if line.startswith('+') and not line.startswith('+++'):
            added += 1
        elif line.startswith('-') and not line.startswith('---'):
            removed += 1
    return added, removed


# ---------------------------------------------------------------------------
# DEFAULT_IGNORE for repo listing (same as file_merger.py)
# ---------------------------------------------------------------------------

_DEFAULT_IGNORE = {
    '.git', '__pycache__', 'node_modules', '.venv', 'venv', '.env',
    '.tox', '.mypy_cache', '.pytest_cache', 'dist', 'build', '.eggs',
    '.idea', '.vscode', '.vs',
}


# ---------------------------------------------------------------------------
# Tool: checkout_file
# ---------------------------------------------------------------------------

def checkout_file(path: str, session_id: str, repo_path: str,
                  workspace_path: str, **kwargs) -> str:
    """Copy a file from the repo to the agent's workspace.
    Records the checkout in session_files for later diff tracking."""
    try:
        # Validate path stays within repo
        repo_resolved = _validate_path(path, repo_path)
        if repo_resolved is None:
            return json.dumps({"error": "Path escapes repository directory"})
        if not os.path.isfile(repo_resolved):
            return json.dumps({"error": f"File not found in repo: {path}"})

        # Create workspace directory structure
        ws_target = os.path.join(workspace_path, path)
        os.makedirs(os.path.dirname(ws_target), exist_ok=True)

        # Copy file preserving timestamps
        shutil.copy2(repo_resolved, ws_target)

        # Compute hash and record checkout
        file_hash = _compute_hash(repo_resolved)
        agent_schema.record_file_checkout(session_id, path, file_hash)

        # Return content preview (first 50 lines)
        preview = ""
        try:
            with open(ws_target, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            line_count = len(lines)
            preview = ''.join(lines[:50])
            if line_count > 50:
                preview += f"\n... ({line_count - 50} more lines)"
        except Exception:
            line_count = 0

        return json.dumps({
            "checked_out": True,
            "path": path,
            "hash": file_hash,
            "lines": line_count,
            "preview": preview
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: checkout_files (batch)
# ---------------------------------------------------------------------------

def checkout_files(paths: list, session_id: str, repo_path: str,
                   workspace_path: str, **kwargs) -> str:
    """Batch checkout multiple files from the repo."""
    try:
        results = []
        errors = []
        for path in paths:
            result = json.loads(
                checkout_file(path, session_id, repo_path, workspace_path)
            )
            if 'error' in result:
                errors.append({"path": path, "error": result['error']})
            else:
                results.append({"path": path, "hash": result['hash']})

        return json.dumps({
            "checked_out": len(results),
            "errors": len(errors),
            "files": results,
            "error_details": errors if errors else None
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: list_repo_files
# ---------------------------------------------------------------------------

def list_repo_files(pattern: str = "*", repo_path: str = ".",
                    **kwargs) -> str:
    """List files in the registered repo matching a glob pattern.
    Read-only — does not checkout any files."""
    try:
        repo_abs = os.path.realpath(repo_path)
        if not os.path.isdir(repo_abs):
            return json.dumps({"error": f"Repo path not found: {repo_path}"})

        matches = []
        for root, dirs, files in os.walk(repo_abs):
            # Prune ignored directories
            dirs[:] = [d for d in dirs if d not in _DEFAULT_IGNORE]

            for fname in files:
                rel = os.path.relpath(os.path.join(root, fname), repo_abs)
                rel = rel.replace('\\', '/')
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fname, pattern):
                    matches.append(rel)
                    if len(matches) >= 500:
                        return json.dumps({
                            "files": matches,
                            "count": len(matches),
                            "truncated": True
                        })

        return json.dumps({
            "files": matches,
            "count": len(matches),
            "truncated": False
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: get_repo_file_content
# ---------------------------------------------------------------------------

def get_repo_file_content(path: str, repo_path: str = ".",
                          start_line: Optional[int] = None,
                          end_line: Optional[int] = None,
                          **kwargs) -> str:
    """Read a file from the repo WITHOUT checking it out.
    For context-gathering only. Read-only, no copy."""
    try:
        resolved = _validate_path(path, repo_path)
        if resolved is None:
            return json.dumps({"error": "Path escapes repository directory"})
        if not os.path.isfile(resolved):
            return json.dumps({"error": f"File not found: {path}"})

        with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        total = len(lines)
        if start_line is not None or end_line is not None:
            s = (start_line or 1) - 1
            e = end_line or total
            s = max(0, s)
            e = min(total, e)
            lines = lines[s:e]

        content = ''.join(lines)
        return json.dumps({"content": content, "lines": len(lines), "total_lines": total})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: signal_done
# ---------------------------------------------------------------------------

def signal_done(summary: str, session_id: str, **kwargs) -> str:
    """Signal that the agent has completed its task.
    Computes final diff stats for all modified files."""
    try:
        session = agent_schema.get_session(session_id)
        if session is None:
            return json.dumps({"error": f"Session not found: {session_id}"})

        # Get the repo path
        repo = agent_schema.get_repo(session['repo_id'])
        if repo is None:
            return json.dumps({"error": "Associated repo not found"})

        # Compute final stats for all checked-out files
        files = agent_schema.get_session_files(session_id)
        ws = session['workspace_path']
        modified_count = 0
        total_added = 0
        total_removed = 0

        for f in files:
            ws_file = os.path.join(ws, f['relative_path'])
            if not os.path.isfile(ws_file):
                # File was deleted by agent
                agent_schema.update_file_stats(
                    session_id, f['relative_path'],
                    current_hash='', lines_added=0, lines_removed=0,
                    status='deleted'
                )
                continue

            current_hash = _compute_hash(ws_file)
            if current_hash != f['checkout_hash']:
                # File was modified
                repo_file = os.path.join(repo['repo_path'], f['relative_path'])
                try:
                    with open(ws_file, 'r', encoding='utf-8', errors='replace') as fh:
                        new_content = fh.read()
                    added, removed = _compute_diff_stats(repo_file, new_content)
                except Exception:
                    added, removed = 0, 0

                agent_schema.update_file_stats(
                    session_id, f['relative_path'],
                    current_hash=current_hash,
                    lines_added=added, lines_removed=removed,
                    status='modified'
                )
                modified_count += 1
                total_added += added
                total_removed += removed

        # Also check for new files (created in workspace but not checked out)
        for root, dirs, filenames in os.walk(ws):
            dirs[:] = [d for d in dirs if d not in _DEFAULT_IGNORE]
            for fname in filenames:
                abs_path = os.path.join(root, fname)
                rel_path = os.path.relpath(abs_path, ws).replace('\\', '/')
                # Check if this file was already tracked
                existing = [f for f in files if f['relative_path'] == rel_path]
                if not existing:
                    file_hash = _compute_hash(abs_path)
                    try:
                        with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
                            line_count = sum(1 for _ in fh)
                    except Exception:
                        line_count = 0
                    agent_schema.record_new_file(
                        session_id, rel_path, file_hash, line_count
                    )
                    modified_count += 1
                    total_added += line_count

        # Update session status
        agent_schema.update_session_status(session_id, 'completed')

        # Save summary as conversation message
        agent_schema.save_conversation_message(
            session_id, 'assistant',
            f"Task completed. Summary: {summary}"
        )

        return json.dumps({
            "completed": True,
            "session_id": session_id,
            "summary": summary,
            "files_modified": modified_count,
            "total_lines_added": total_added,
            "total_lines_removed": total_removed
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: run_command
# ---------------------------------------------------------------------------

def run_command(command: str, session_id: str, workspace_path: str,
                timeout: int = 120, **kwargs) -> str:
    """Run a shell command in the agent's workspace.
    Checks against allowed_commands unless allow_free_commands is enabled."""
    try:
        session = agent_schema.get_session(session_id)
        if session is None:
            return json.dumps({"error": f"Session not found: {session_id}"})

        repo = agent_schema.get_repo(session['repo_id'])
        if repo is None:
            return json.dumps({"error": "Associated repo not found"})

        # Check command permissions
        if not repo['allow_free_commands']:
            allowed = repo['allowed_commands']
            if not allowed:
                return json.dumps({
                    "error": "No commands are allowed for this repo. "
                             "Configure allowed_commands in repo settings."
                })
            # Check if command starts with any allowed command
            command_allowed = False
            for allowed_cmd in allowed:
                if command.strip().startswith(allowed_cmd.strip()):
                    command_allowed = True
                    break
            if not command_allowed:
                return json.dumps({
                    "error": f"Command not allowed. Permitted commands: {allowed}"
                })

        # Execute in workspace
        result = subprocess.run(
            command,
            shell=True,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = result.stdout
        if len(output) > 10000:
            output = output[:10000] + "\n... (output truncated)"
        stderr = result.stderr
        if len(stderr) > 5000:
            stderr = stderr[:5000] + "\n... (stderr truncated)"

        return json.dumps({
            "exit_code": result.returncode,
            "stdout": output,
            "stderr": stderr,
            "command": command
        })
    except subprocess.TimeoutExpired:
        return json.dumps({
            "error": f"Command timed out after {timeout}s",
            "command": command
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})
