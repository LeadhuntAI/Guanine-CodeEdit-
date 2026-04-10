"""
Agent tool functions for the sandboxed coding agent review system.

These are the single source of truth for agent capabilities.
Co-located agents import them directly; remote agents access them via MCP.
Every function returns a JSON string (same contract as agentic/tools/).

Security:
    - All file paths are validated to prevent directory traversal
    - Commands are checked against a configurable whitelist
    - Output is truncated to prevent memory exhaustion
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import subprocess
from typing import Optional

import agent_schema

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path validation (shared with agentic/tools/)
# ---------------------------------------------------------------------------

def _validate_path(path: str, base_dir: str) -> str | None:
    """Resolve path and ensure it stays within base_dir.

    Returns the resolved absolute path if valid, None if the path
    would escape the base directory (e.g., via '../' traversal).
    """
    resolved = os.path.realpath(os.path.join(base_dir, path))
    base = os.path.realpath(base_dir)
    if not resolved.startswith(base + os.sep) and resolved != base:
        return None
    return resolved


def _compute_hash(filepath: str) -> str:
    """SHA-256 hash of a file (64KB chunked reads for memory efficiency)."""
    h = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
    except (OSError, PermissionError):
        return "ERROR"
    return h.hexdigest()


def _compute_hash_str(content: str) -> str:
    """SHA-256 hash of a string."""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _compute_diff_stats(original_path: str, new_content: str) -> tuple[int, int]:
    """Compute lines added/removed between original file and new content.

    Returns:
        Tuple of (lines_added, lines_removed).
    """
    try:
        with open(original_path, 'r', encoding='utf-8', errors='replace') as f:
            original_lines = f.readlines()
    except (OSError, PermissionError):
        original_lines = []

    new_lines = new_content.splitlines(keepends=True)
    diff = list(difflib.unified_diff(original_lines, new_lines, n=0))

    added = sum(1 for line in diff if line.startswith('+') and not line.startswith('+++'))
    removed = sum(1 for line in diff if line.startswith('-') and not line.startswith('---'))
    return added, removed


# ---------------------------------------------------------------------------
# DEFAULT_IGNORE for repo listing (same as file_merger.py)
# ---------------------------------------------------------------------------

_DEFAULT_IGNORE = {
    '.git', '__pycache__', 'node_modules', '.venv', 'venv', '.env',
    '.tox', '.mypy_cache', '.pytest_cache', 'dist', 'build', '.eggs',
    '.idea', '.vscode', '.vs', '.originals',
}

# Maximum file listing results
_MAX_FILE_RESULTS = 500

# Maximum output sizes for run_command
_MAX_STDOUT = 10000
_MAX_STDERR = 5000

# Maximum preview lines for checkout
_PREVIEW_LINES = 50


# ---------------------------------------------------------------------------
# Tool: checkout_file
# ---------------------------------------------------------------------------

def checkout_file(path: str, session_id: str, repo_path: str,
                  workspace_path: str, **kwargs) -> str:
    """Copy a file from the repo to the agent's workspace.

    Records the checkout in session_files for later diff tracking.
    Returns a JSON object with checkout confirmation, hash, line count,
    and a preview of the first 50 lines.
    """
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

        # Return content preview
        preview = ""
        line_count = 0
        try:
            with open(ws_target, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            line_count = len(lines)
            preview = ''.join(lines[:_PREVIEW_LINES])
            if line_count > _PREVIEW_LINES:
                preview += f"\n... ({line_count - _PREVIEW_LINES} more lines)"
        except Exception:
            pass

        logger.info("Checked out %s (%d lines, hash=%s...)", path, line_count, file_hash[:8])
        return json.dumps({
            "checked_out": True,
            "path": path,
            "workspace_file_path": os.path.abspath(ws_target),
            "hash": file_hash,
            "lines": line_count,
            "preview": preview
        })
    except Exception as exc:
        logger.error("Checkout failed for %s: %s", path, exc)
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: checkout_files (batch)
# ---------------------------------------------------------------------------

def checkout_files(paths: list, session_id: str, repo_path: str,
                   workspace_path: str, **kwargs) -> str:
    """Batch checkout multiple files from the repo.

    Returns a summary with successful checkouts and any errors.
    """
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

    Read-only — does not checkout any files. Results are capped
    at _MAX_FILE_RESULTS entries to prevent memory exhaustion.
    """
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
                    if len(matches) >= _MAX_FILE_RESULTS:
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

    For context-gathering only. Supports optional 1-based line range
    for reading specific sections of large files.
    """
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
            s = max(0, (start_line or 1) - 1)
            e = min(total, end_line or total)
            lines = lines[s:e]

        content = ''.join(lines)
        return json.dumps({
            "content": content,
            "lines": len(lines),
            "total_lines": total,
            "path": path
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: signal_done
# ---------------------------------------------------------------------------

def signal_done(summary: str, session_id: str, **kwargs) -> str:
    """Signal that the agent has completed its task.

    Performs finalization:
    1. Computes final diff stats for all tracked files
    2. Detects new files created in workspace but not tracked
    3. Detects deleted files (checked out but removed)
    4. Updates session status to 'completed'
    5. Saves the summary as a conversation message
    """
    try:
        session = agent_schema.get_session(session_id)
        if session is None:
            return json.dumps({"error": f"Session not found: {session_id}"})

        repo = agent_schema.get_repo(session['repo_id'])
        if repo is None:
            return json.dumps({"error": "Associated repo not found"})

        # Compute final stats for all checked-out files
        files = agent_schema.get_session_files(session_id)
        ws = session['workspace_path']
        modified_count = 0
        deleted_count = 0
        new_count = 0
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
                deleted_count += 1
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
                logger.info("Modified: %s (+%d/-%d)", f['relative_path'], added, removed)

        # Check for new files (created in workspace but not checked out)
        tracked_paths = {f['relative_path'] for f in files}
        for root, dirs, filenames in os.walk(ws):
            dirs[:] = [d for d in dirs if d not in _DEFAULT_IGNORE]
            for fname in filenames:
                abs_path = os.path.join(root, fname)
                rel_path = os.path.relpath(abs_path, ws).replace('\\', '/')
                if rel_path not in tracked_paths:
                    file_hash = _compute_hash(abs_path)
                    try:
                        with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
                            line_count = sum(1 for _ in fh)
                    except Exception:
                        line_count = 0
                    agent_schema.record_new_file(
                        session_id, rel_path, file_hash, line_count
                    )
                    new_count += 1
                    total_added += line_count
                    logger.info("New file: %s (%d lines)", rel_path, line_count)

        # Update session status
        agent_schema.update_session_status(session_id, 'completed')

        # Save summary as conversation message
        agent_schema.save_conversation_message(
            session_id, 'assistant',
            f"Task completed. Summary: {summary}"
        )

        logger.info(
            "Session %s done: %d modified, %d new, %d deleted, +%d/-%d lines",
            session_id, modified_count, new_count, deleted_count,
            total_added, total_removed
        )
        return json.dumps({
            "completed": True,
            "session_id": session_id,
            "summary": summary,
            "files_modified": modified_count,
            "files_new": new_count,
            "files_deleted": deleted_count,
            "total_lines_added": total_added,
            "total_lines_removed": total_removed
        })
    except Exception as exc:
        logger.error("signal_done failed for %s: %s", session_id, exc)
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Tool: run_command
# ---------------------------------------------------------------------------

def run_command(command: str, session_id: str, workspace_path: str,
                timeout: int = 120, **kwargs) -> str:
    """Run a shell command in the agent's workspace.

    Security: Commands are checked against the repo's allowed_commands
    whitelist unless allow_free_commands is enabled. Output is truncated
    to prevent memory exhaustion.

    Args:
        command: Shell command to execute.
        session_id: The active session ID.
        workspace_path: Directory to run the command in.
        timeout: Maximum execution time in seconds (default: 120).
    """
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
            cmd_stripped = command.strip()
            if not any(cmd_stripped.startswith(a.strip()) for a in allowed):
                return json.dumps({
                    "error": f"Command not allowed. Permitted commands: {allowed}"
                })

        logger.info("Running command in %s: %s", workspace_path, command[:100])

        # Execute in workspace
        result = subprocess.run(
            command,
            shell=True,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        stdout = result.stdout
        if len(stdout) > _MAX_STDOUT:
            stdout = stdout[:_MAX_STDOUT] + "\n... (output truncated)"
        stderr = result.stderr
        if len(stderr) > _MAX_STDERR:
            stderr = stderr[:_MAX_STDERR] + "\n... (stderr truncated)"

        return json.dumps({
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "command": command
        })
    except subprocess.TimeoutExpired:
        logger.warning("Command timed out after %ds: %s", timeout, command[:100])
        return json.dumps({
            "error": f"Command timed out after {timeout}s",
            "command": command
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Reconciliation: bridge backend file changes into Guanine review system
# ---------------------------------------------------------------------------

def reconcile_session(session_id: str) -> dict:
    """Reconcile file changes for a completed agent session.

    For any backend, this:
    1. Walks the workspace directory to detect all changes
    2. For OpenCode backend: also fetches diff via the API
    3. Compares hashes against checkout records
    4. Updates session_files with diff stats
    5. Transitions status to 'completed' if not already

    This bridges agent file changes (regardless of backend) into
    Guanine's review system. After reconciliation, the standard
    review flow (inline diff, accept/reject, merge-to-repo) works.

    Returns:
        Dict with reconciliation results.
    """
    session = agent_schema.get_session(session_id)
    if session is None:
        return {"error": f"Session not found: {session_id}"}

    repo = agent_schema.get_repo(session['repo_id'])
    if repo is None:
        return {"error": "Associated repo not found"}

    ws = session['workspace_path']
    backend_name = session.get('backend', 'builtin')
    backend_ref = session.get('backend_session_id')

    modified_count = 0
    deleted_count = 0
    new_count = 0
    total_added = 0
    total_removed = 0

    # --- Step 0: Ensure .originals/ directory for baseline snapshots ---
    originals_dir = os.path.join(ws, '.originals')

    # --- Step 1: Reconcile already-tracked files ---
    files = agent_schema.get_session_files(session_id)
    tracked_paths = {f['relative_path'] for f in files}

    for f in files:
        ws_file = os.path.join(ws, f['relative_path'])
        if not os.path.isfile(ws_file):
            agent_schema.update_file_stats(
                session_id, f['relative_path'],
                current_hash='', lines_added=0, lines_removed=0,
                status='deleted'
            )
            deleted_count += 1
            continue

        current_hash = _compute_hash(ws_file)
        if current_hash != f['checkout_hash']:
            repo_file = os.path.join(repo['repo_path'], f['relative_path'])

            # Snapshot the original repo file into .originals/ if not already
            # done, so inline diffs always have a stable baseline even after
            # other sessions merge changes to the repo.
            orig_file = os.path.join(originals_dir, f['relative_path'])
            if not os.path.isfile(orig_file) and os.path.isfile(repo_file):
                os.makedirs(os.path.dirname(orig_file), exist_ok=True)
                shutil.copy2(repo_file, orig_file)

            # Use .originals/ as diff baseline if available (stable), else repo
            diff_baseline = orig_file if os.path.isfile(orig_file) else repo_file
            try:
                with open(ws_file, 'r', encoding='utf-8', errors='replace') as fh:
                    new_content = fh.read()
                added, removed = _compute_diff_stats(diff_baseline, new_content)
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

    # --- Step 2: Detect new files in workspace ---
    if os.path.isdir(ws):
        for root, dirs, filenames in os.walk(ws):
            dirs[:] = [d for d in dirs if d not in _DEFAULT_IGNORE]
            for fname in filenames:
                abs_path = os.path.join(root, fname)
                rel_path = os.path.relpath(abs_path, ws).replace('\\', '/')
                if rel_path not in tracked_paths:
                    file_hash = _compute_hash(abs_path)
                    try:
                        with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
                            line_count = sum(1 for _ in fh)
                    except Exception:
                        line_count = 0
                    agent_schema.record_new_file(
                        session_id, rel_path, file_hash, line_count
                    )
                    new_count += 1
                    total_added += line_count

    # --- Step 3: For OpenCode backend, try fetching diff metadata ---
    opencode_diff = None
    if backend_name == 'opencode' and backend_ref:
        try:
            from agentic.engine.opencode_client import OpenCodeClient
            client = OpenCodeClient()
            opencode_diff = client.get_diff(backend_ref)
            logger.info("Fetched OpenCode diff for session %s", session_id)
        except Exception as e:
            logger.warning("Could not fetch OpenCode diff: %s", e)

    # --- Step 4: Update status if not already completed ---
    if session['status'] in ('running', 'pending'):
        agent_schema.update_session_status(session_id, 'completed')

    result = {
        "reconciled": True,
        "session_id": session_id,
        "backend": backend_name,
        "files_modified": modified_count,
        "files_new": new_count,
        "files_deleted": deleted_count,
        "total_lines_added": total_added,
        "total_lines_removed": total_removed,
    }
    if opencode_diff:
        result["opencode_diff"] = opencode_diff

    logger.info(
        "Reconciled session %s: %d modified, %d new, %d deleted, +%d/-%d",
        session_id, modified_count, new_count, deleted_count,
        total_added, total_removed
    )
    return result


# ---------------------------------------------------------------------------
# Reconcile OpenCode iframe sessions (files edited directly in repo)
# ---------------------------------------------------------------------------

def _compute_diff_stats_from_strings(original: str, modified: str) -> tuple:
    """Compute (lines_added, lines_removed) from two strings."""
    orig_lines = original.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, orig_lines, mod_lines
    ).get_opcodes():
        if tag == 'replace':
            removed += i2 - i1
            added += j2 - j1
        elif tag == 'delete':
            removed += i2 - i1
        elif tag == 'insert':
            added += j2 - j1
    return added, removed


def reconcile_opencode_session(session_id: str) -> dict:
    """Reconcile an OpenCode iframe session by detecting repo changes via git.

    OpenCode edits files directly in the repo (not in the Guanine workspace).
    This function:
    1. Detects changed files via git diff + untracked files
    2. Copies modified files to the workspace
    3. Saves original (git HEAD) versions to workspace/.originals/
    4. Records everything in session_files for the review system
    """
    session = agent_schema.get_session(session_id)
    if session is None:
        return {"error": f"Session not found: {session_id}"}

    repo = agent_schema.get_repo(session['repo_id'])
    if repo is None:
        return {"error": "Associated repo not found"}

    repo_path = repo['repo_path']
    ws = session['workspace_path']
    originals_dir = os.path.join(ws, '.originals')

    # Try syncing with the most recent active OpenCode session (non-blocking)
    backend_ref = session.get('backend_session_id')
    try:
        _sync_opencode_session_id(session_id, repo_path, backend_ref)
    except Exception:
        pass  # OpenCode server may be down — git diff is sufficient

    # Step 1: Detect changed files via git
    # Filter to files modified AFTER the session was created, to avoid
    # picking up pre-existing uncommitted changes from development.
    changed_files = []
    new_files = []
    session_created = session.get('created_at', '')

    # Parse session creation time for file mtime filtering
    import datetime
    session_ts = None
    if session_created:
        try:
            # Handle ISO format with timezone
            clean = session_created.replace('+00:00', '').replace('Z', '')
            session_ts = datetime.datetime.fromisoformat(clean).timestamp()
        except Exception:
            pass

    try:
        # Modified/deleted files (tracked by git)
        result = subprocess.run(
            ['git', 'diff', '--name-only', 'HEAD'],
            cwd=repo_path, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            for f in result.stdout.strip().split('\n'):
                f = f.strip()
                if not f:
                    continue
                # Filter by mtime if we have a session timestamp
                if session_ts:
                    fpath = os.path.join(repo_path, f)
                    try:
                        if os.path.isfile(fpath) and os.path.getmtime(fpath) < session_ts:
                            continue  # File was modified before session
                    except OSError:
                        pass
                changed_files.append(f)

        # New untracked files (same filter)
        result = subprocess.run(
            ['git', 'ls-files', '--others', '--exclude-standard'],
            cwd=repo_path, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            for f in result.stdout.strip().split('\n'):
                f = f.strip()
                if not f:
                    continue
                if session_ts:
                    fpath = os.path.join(repo_path, f)
                    try:
                        if os.path.isfile(fpath) and os.path.getmtime(fpath) < session_ts:
                            continue
                    except OSError:
                        pass
                new_files.append(f)
    except Exception as e:
        logger.warning("Git diff failed, trying OpenCode API: %s", e)

    # Fallback: try OpenCode diff API (only if server is reachable)
    if not changed_files and not new_files:
        try:
            from agent_backends import get_repo_server
            server = get_repo_server(session['repo_id'])
            if server:
                # Quick connectivity check
                import urllib.request
                urllib.request.urlopen(server['base_url'], timeout=2)
                from agentic.engine.opencode_client import OpenCodeClient
                client = OpenCodeClient(server['base_url'])
                # Find the most recent active session
                oc_sessions = client.list_sessions()
                for oc_s in oc_sessions:
                    summary = oc_s.get('summary', {})
                    if summary.get('files', 0) > 0:
                        diff = client.get_diff(oc_s['id'])
                        for d in diff:
                            f = d.get('file', '')
                            if f:
                                changed_files.append(f)
                        break
        except Exception as e:
            logger.warning("OpenCode diff fallback failed: %s", e)

    if not changed_files and not new_files:
        return {
            "reconciled": True,
            "session_id": session_id,
            "files_modified": 0,
            "files_new": 0,
            "message": "No changes detected in repo",
        }

    # Filter out non-source files (logs, index files, etc.)
    skip_patterns = {'.code-index', '.claude/jcodemunch', '__pycache__',
                     'node_modules', '.git', 'sessions/'}
    def should_include(path):
        return not any(skip in path for skip in skip_patterns)

    changed_files = [f for f in changed_files if should_include(f)]
    new_files = [f for f in new_files if should_include(f)]

    modified_count = 0
    new_count = 0
    total_added = 0
    total_removed = 0

    # Step 2: Process modified files
    for rel_path in changed_files:
        repo_file = os.path.join(repo_path, rel_path)
        ws_file = os.path.join(ws, rel_path)
        orig_file = os.path.join(originals_dir, rel_path)

        if not os.path.isfile(repo_file):
            continue  # File was deleted

        # Copy modified file to workspace
        os.makedirs(os.path.dirname(ws_file), exist_ok=True)
        shutil.copy2(repo_file, ws_file)

        # Save original from git HEAD
        try:
            git_show = subprocess.run(
                ['git', 'show', f'HEAD:{rel_path}'],
                cwd=repo_path, capture_output=True, timeout=10
            )
            if git_show.returncode == 0:
                os.makedirs(os.path.dirname(orig_file), exist_ok=True)
                with open(orig_file, 'wb') as fh:
                    fh.write(git_show.stdout)
        except Exception as e:
            logger.warning("Could not save original for %s: %s", rel_path, e)

        # Compute diff stats
        try:
            orig_content = ''
            if os.path.isfile(orig_file):
                with open(orig_file, 'r', encoding='utf-8', errors='replace') as fh:
                    orig_content = fh.read()
            with open(ws_file, 'r', encoding='utf-8', errors='replace') as fh:
                new_content = fh.read()
            added, removed = _compute_diff_stats_from_strings(orig_content, new_content)
        except Exception:
            added, removed = 0, 0

        # Record in session_files
        orig_hash = _compute_hash(orig_file) if os.path.isfile(orig_file) else ''
        current_hash = _compute_hash(ws_file)
        agent_schema.record_file_checkout(session_id, rel_path, orig_hash)
        agent_schema.update_file_stats(
            session_id, rel_path, current_hash, added, removed, 'modified'
        )
        modified_count += 1
        total_added += added
        total_removed += removed

    # Step 3: Process new (untracked) files
    for rel_path in new_files:
        repo_file = os.path.join(repo_path, rel_path)
        ws_file = os.path.join(ws, rel_path)

        if not os.path.isfile(repo_file):
            continue

        os.makedirs(os.path.dirname(ws_file), exist_ok=True)
        shutil.copy2(repo_file, ws_file)

        try:
            with open(ws_file, 'r', encoding='utf-8', errors='replace') as fh:
                line_count = sum(1 for _ in fh)
        except Exception:
            line_count = 0

        file_hash = _compute_hash(ws_file)
        agent_schema.record_new_file(session_id, rel_path, file_hash, line_count)
        new_count += 1
        total_added += line_count

    # Step 4: Update session status
    if session['status'] in ('running', 'pending'):
        agent_schema.update_session_status(session_id, 'completed')

    result = {
        "reconciled": True,
        "session_id": session_id,
        "files_modified": modified_count,
        "files_new": new_count,
        "total_lines_added": total_added,
        "total_lines_removed": total_removed,
    }
    logger.info(
        "OpenCode reconcile %s: %d modified, %d new, +%d/-%d",
        session_id, modified_count, new_count, total_added, total_removed
    )
    return result


def _sync_opencode_session_id(session_id: str, repo_path: str,
                               current_ref: Optional[str]) -> None:
    """Sync Guanine's backend_session_id with the most recently active
    OpenCode session that has actual file changes."""
    try:
        from agent_backends import get_repo_server
        session = agent_schema.get_session(session_id)
        if not session:
            return
        server = get_repo_server(session['repo_id'])
        if not server:
            return
        # Quick connectivity check (2s timeout)
        import urllib.request
        try:
            urllib.request.urlopen(server['base_url'], timeout=2)
        except Exception:
            logger.debug("OpenCode server not reachable, skipping sync")
            return
        from agentic.engine.opencode_client import OpenCodeClient
        client = OpenCodeClient(server['base_url'])
        oc_sessions = client.list_sessions()
        if not oc_sessions:
            return

        # Find most recently updated session with file changes
        best = None
        for s in oc_sessions:
            summary = s.get('summary', {})
            if summary.get('files', 0) > 0:
                updated = s.get('time', {}).get('updated', 0)
                if best is None or updated > best.get('time', {}).get('updated', 0):
                    best = s

        if best and best['id'] != current_ref:
            db = agent_schema.get_agent_db()
            db.execute(
                'UPDATE agent_sessions SET backend_session_id = ? WHERE session_id = ?',
                (best['id'], session_id)
            )
            db.commit()
            logger.info("Synced OpenCode session: %s -> %s", session_id, best['id'])
    except Exception as e:
        logger.debug("OpenCode session sync skipped: %s", e)


# ---------------------------------------------------------------------------
# Tool: spawn_agent — orchestrator creates sub-sessions
# ---------------------------------------------------------------------------

def spawn_agent(task: str, parent_session_id: str,
                backend: str = 'builtin', agent: str = 'build',
                files: Optional[list] = None, **kwargs) -> str:
    """Spawn a sub-agent session for parallel task execution.

    Creates a child session linked to the parent via parent_session_id.
    The child gets its own isolated workspace. Optionally pre-checks out
    files from the repo into the child workspace.

    Args:
        task: Description of what the sub-agent should do.
        parent_session_id: ID of the parent orchestrator session.
        backend: Agent backend ('builtin', 'opencode').
        agent: Agent type ('build', 'plan', 'explore').
        files: Optional list of relative file paths to pre-checkout.

    Returns:
        JSON string with sub_session_id, workspace_path, status.
    """
    try:
        parent = agent_schema.get_session(parent_session_id)
        if parent is None:
            return json.dumps({"error": f"Parent session not found: {parent_session_id}"})

        repo = agent_schema.get_repo(parent['repo_id'])
        if repo is None:
            return json.dumps({"error": "Parent repo not found"})

        # Create child session
        child = agent_schema.create_session(
            repo_id=parent['repo_id'],
            task_description=task,
            agent_model=agent,
            backend=backend,
            parent_session_id=parent_session_id,
        )

        child_ws = child['workspace_path']
        repo_path = repo['repo_path']

        # Pre-checkout requested files
        checked_out = []
        if files:
            for rel_path in files:
                src = os.path.join(repo_path, rel_path)
                dst = os.path.join(child_ws, rel_path)
                if os.path.isfile(src):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    file_hash = _compute_hash(src)
                    agent_schema.record_file_checkout(
                        child['session_id'], rel_path, file_hash
                    )
                    checked_out.append(rel_path)

        # Start the backend session if using an external backend
        backend_session_id = None
        if backend != 'builtin':
            try:
                from agent_backends import get_backend
                be = get_backend(backend)
                backend_session_id = be.start_session(child_ws, task, agent_type=agent)
                # Update the child session with the backend ref
                db = agent_schema.get_agent_db()
                db.execute(
                    'UPDATE agent_sessions SET backend_session_id = ? WHERE session_id = ?',
                    (backend_session_id, child['session_id'])
                )
                db.commit()
            except Exception as e:
                logger.warning("Failed to start backend session: %s", e)

        # Transition to running
        agent_schema.update_session_status(child['session_id'], 'running')

        logger.info(
            "Spawned sub-agent %s (parent=%s, backend=%s, agent=%s, files=%d)",
            child['session_id'], parent_session_id, backend, agent, len(checked_out)
        )

        return json.dumps({
            "sub_session_id": child['session_id'],
            "workspace_path": child_ws,
            "backend": backend,
            "agent": agent,
            "backend_session_id": backend_session_id,
            "files_checked_out": checked_out,
            "status": "running"
        })
    except Exception as exc:
        logger.error("spawn_agent failed: %s", exc)
        return json.dumps({"error": str(exc)})
