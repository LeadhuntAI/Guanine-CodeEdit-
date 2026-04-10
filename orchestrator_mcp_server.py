"""
Orchestrator MCP Server for Guanine CodeEdit.

Exposes tools for EXTERNAL planning/orchestration agents (Claude Code, Cursor,
etc.) to submit coding tasks that OpenCode executes autonomously in sandboxed
sessions. The human reviews completed work through the Guanine IDE.

Flow:
    External agent → submit_task("fix auth bug", repo_id)
    → Guanine creates session → starts OpenCode → sends task
    → OpenCode works in sandbox → signals done → diffs computed
    → Human reviews at http://localhost:5000/agent/sessions

IMPORTANT: This server must NEVER be exposed to OpenCode itself.
OpenCode only sees the sandbox MCP server (agent_mcp_server.py).

Usage:
    # stdio transport (for Claude Code / Cursor)
    python orchestrator_mcp_server.py

    # streamable HTTP transport
    python orchestrator_mcp_server.py --transport streamable-http --port 8081

Configure in your MCP client (.mcp.json / Claude settings):
    {
        "mcpServers": {
            "guanine-orchestrator": {
                "command": "python",
                "args": ["C:/path/to/orchestrator_mcp_server.py"]
            }
        }
    }
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety guard: refuse to run inside an OpenCode sandbox
# ---------------------------------------------------------------------------

_INSIDE_OPENCODE = bool(os.environ.get('GUANINE_SESSION_ID', '').strip())

if _INSIDE_OPENCODE:
    print(
        "ERROR: orchestrator_mcp_server.py must NOT run inside an OpenCode "
        "session. This server is for external orchestration agents only.",
        file=sys.stderr,
    )
    sys.exit(1)


from mcp.server.fastmcp import FastMCP

import agent_schema

mcp = FastMCP(
    name="guanine-orchestrator",
    instructions=(
        "Guanine CodeEdit orchestrator. Use this to delegate coding tasks to "
        "AI agents that work in sandboxed sessions. You do NOT edit code "
        "yourself — you submit tasks, monitor progress, and review results.\n\n"
        "Workflow:\n"
        "1. Call list_repos() to see available repositories\n"
        "2. Call submit_task(repo_id, task) to delegate a coding task\n"
        "3. Call get_task_status(task_id) to check progress\n"
        "4. When status is 'completed', call get_task_result(task_id) for details\n"
        "5. The human reviews and merges changes through the Guanine web UI\n\n"
        "You can submit multiple tasks in parallel across repos."
    ),
)

# ---------------------------------------------------------------------------
# Task tracking
# ---------------------------------------------------------------------------

_active_tasks: dict[str, dict] = {}  # task_id -> task info
_tasks_lock = threading.Lock()

_TASK_TIMEOUT_SECONDS = 30 * 60  # 30 minutes default


def _update_task(task_id: str, **kwargs):
    """Thread-safe update of task tracking dict."""
    with _tasks_lock:
        task = _active_tasks.get(task_id)
        if task:
            task.update(kwargs)


def _safe_complete_task(task_id: str, session_id: str):
    """Idempotent task completion — safe to call from both SSE monitor and signal_done."""
    with _tasks_lock:
        task = _active_tasks.get(task_id)
        if not task or task.get('status') in ('completed', 'failed'):
            return
        task['status'] = 'completing'

    session = agent_schema.get_session(session_id)
    if session and session['status'] not in ('completed', 'review', 'merged'):
        try:
            from agent_tools import reconcile_session
            reconcile_session(session_id)
        except Exception as e:
            logger.warning("Reconcile failed for %s: %s", session_id, e)
        agent_schema.update_session_status(session_id, 'completed')

    try:
        from agent_review import _create_review_session
        _create_review_session(session_id)
    except Exception as e:
        logger.debug("Review session creation skipped for %s: %s", session_id, e)

    _update_task(task_id, status='completed', completed_at=time.time())


def _monitor_task(task_id: str, session_id: str, backend_session_id: str,
                  base_url: str, password: Optional[str] = None):
    """Background thread: watches OpenCode SSE events for task completion."""
    start_time = time.time()
    try:
        from agentic.engine.opencode_client import OpenCodeClient
        client = OpenCodeClient(base_url, password=password)

        for event in client.stream_events(backend_session_id):
            # Check timeout
            if time.time() - start_time > _TASK_TIMEOUT_SECONDS:
                logger.warning("Task %s timed out after %ds", task_id, _TASK_TIMEOUT_SECONDS)
                _update_task(task_id, status='stalled')
                agent_schema.update_session_status(session_id, 'completed',
                                                   error_message='Timed out')
                return

            # Check if already completed via signal_done MCP path
            with _tasks_lock:
                task = _active_tasks.get(task_id)
                if task and task.get('status') in ('completed', 'failed'):
                    return

            event_type = event.get('type', '')

            # Detect completion
            if event_type in ('session.complete', 'session.updated'):
                status = (event.get('status')
                          or event.get('properties', {}).get('status', ''))
                if status in ('complete', 'completed'):
                    _safe_complete_task(task_id, session_id)
                    return

            elif event_type == 'session.error':
                _update_task(task_id, status='failed',
                             error=event.get('error', 'Unknown error'))
                agent_schema.update_session_status(session_id, 'rejected',
                                                   error_message=str(event.get('error', '')))
                return

    except Exception as e:
        logger.exception("Task monitor failed for %s", task_id)
        _update_task(task_id, status='failed', error=str(e))


def _resume_running_tasks():
    """On startup, resume monitoring for any running OpenCode sessions."""
    try:
        sessions = agent_schema.list_sessions(status='running')
        for s in sessions:
            if s.get('backend') != 'opencode' or not s.get('backend_session_id'):
                continue
            session_id = s['session_id']
            # Check if OpenCode server is still alive
            from agent_backends import get_repo_server
            server_info = get_repo_server(s.get('repo_id', ''))
            if not server_info:
                continue

            task_id = session_id  # Use session_id as task_id
            with _tasks_lock:
                if task_id in _active_tasks:
                    continue
                _active_tasks[task_id] = {
                    'task_id': task_id,
                    'session_id': session_id,
                    'repo_id': s.get('repo_id', ''),
                    'status': 'running',
                    'submitted_at': time.time(),
                    'task_description': s.get('task_description', ''),
                }

            t = threading.Thread(
                target=_monitor_task,
                args=(task_id, session_id, s['backend_session_id'],
                      server_info['base_url']),
                daemon=True,
            )
            t.start()
            logger.info("Resumed monitoring for task %s", task_id)
    except Exception:
        pass


_resume_running_tasks()


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_repos() -> str:
    """List all registered repositories available for task submission.

    Returns repo_id, name, path, and whether an OpenCode server is currently
    running for each repo."""
    from agent_backends import get_repo_server
    repos = agent_schema.list_repos()
    result = []
    for r in repos:
        server = get_repo_server(r['repo_id'])
        result.append({
            'repo_id': r['repo_id'],
            'repo_name': r['repo_name'],
            'repo_path': r['repo_path'],
            'server_running': server is not None,
            'server_url': server['base_url'] if server else None,
        })
    return json.dumps(result, indent=2)


@mcp.tool()
def get_repo_status(repo_id: str) -> str:
    """Get detailed status for a repo: active sessions, running server, etc."""
    repo = agent_schema.get_repo(repo_id)
    if not repo:
        return json.dumps({'error': f'Repo not found: {repo_id}'})

    from agent_backends import get_repo_server
    server = get_repo_server(repo_id)
    sessions = agent_schema.list_sessions(repo_id=repo_id)

    active = [s for s in sessions if s['status'] in ('pending', 'running')]
    completed = [s for s in sessions if s['status'] == 'completed']
    review = [s for s in sessions if s['status'] in ('review', 'merged')]

    return json.dumps({
        'repo_id': repo_id,
        'repo_name': repo['repo_name'],
        'repo_path': repo['repo_path'],
        'server_running': server is not None,
        'active_tasks': len(active),
        'completed_awaiting_review': len(completed),
        'reviewed': len(review),
        'total_sessions': len(sessions),
    }, indent=2)


@mcp.tool()
def submit_task(repo_id: str, task: str, model: str = "",
                agent_type: str = "build") -> str:
    """Submit a coding task to be executed by OpenCode in a sandboxed session.

    Guanine will:
    1. Create a sandboxed agent session
    2. Start/reuse the OpenCode server for the repo
    3. Create an OpenCode session and send the task
    4. OpenCode works autonomously (checkout, edit, signal_done)

    The task runs asynchronously. Poll with get_task_status().
    When status is 'completed', the human reviews at the Guanine web UI.

    Args:
        repo_id: Repository ID (from list_repos).
        task: Natural language description of the coding task.
        model: Optional model override (default: repo's configured model).
        agent_type: OpenCode agent type ('build' or 'plan'). Default 'build'.

    Returns:
        JSON with task_id, status, and session details.
    """
    import agent_backends
    from agentic.engine.opencode_client import OpenCodeClient

    # Validate
    repo = agent_schema.get_repo(repo_id)
    if not repo:
        return json.dumps({'error': f'Repo not found: {repo_id}'})

    if not task.strip():
        return json.dumps({'error': 'Task description cannot be empty'})

    # Get API key
    settings = agent_backends.get_repo_settings(repo_id)
    api_key = (settings.get('openrouter_api_key', '')
               or os.environ.get('OPENROUTER_API_KEY', ''))
    password = settings.get('opencode_password') or None

    if not api_key:
        return json.dumps({'error': 'No API key configured for this repo'})

    model_id = model or settings.get('default_model', '') or 'openrouter/z-ai/glm-5.1'
    task_label = task[:60]

    try:
        # 1. Create Guanine session
        session = agent_schema.create_session(
            repo_id=repo_id,
            task_description=task_label,
            agent_model=model_id,
            backend='opencode',
        )
        session_id = session['session_id']

        # 2. Ensure OpenCode server is running
        server_info = agent_backends.get_or_start_repo_server(
            repo_id, api_key=api_key, password=password
        )

        # 3. Create OpenCode session
        client = OpenCodeClient(server_info['base_url'], password=password)
        oc_session = client.create_session(repo['repo_path'], title=task_label)
        oc_session_id = oc_session.get('id', '')

        # 4. Link backend session
        db = agent_schema.get_agent_db()
        db.execute(
            'UPDATE agent_sessions SET backend_session_id = ?, status = ? '
            'WHERE session_id = ?',
            (oc_session_id, 'running', session_id)
        )
        db.commit()

        # 5. Send task with sandbox prefix
        from agent_review import _SANDBOX_PREFIX
        actual_message = _SANDBOX_PREFIX + '\n\n' + task
        client.send_message(oc_session_id, actual_message,
                            agent=agent_type if agent_type != 'build' else None)

        # Mark sandbox prefix as sent
        db.execute(
            'UPDATE agent_sessions SET external_context = ? WHERE session_id = ?',
            ('sandbox_prefix_sent', session_id)
        )
        db.commit()

        # Record the message in conversation history
        agent_schema.save_conversation_message(session_id, 'user', task)

        # 6. Track task and start monitor
        task_id = session_id
        with _tasks_lock:
            _active_tasks[task_id] = {
                'task_id': task_id,
                'session_id': session_id,
                'repo_id': repo_id,
                'backend_session_id': oc_session_id,
                'status': 'running',
                'submitted_at': time.time(),
                'task_description': task_label,
            }

        monitor = threading.Thread(
            target=_monitor_task,
            args=(task_id, session_id, oc_session_id,
                  server_info['base_url'], password),
            daemon=True,
        )
        monitor.start()

        logger.info("Task submitted: %s -> session %s -> OpenCode %s",
                     task_label, session_id, oc_session_id)

        return json.dumps({
            'task_id': task_id,
            'session_id': session_id,
            'backend_session_id': oc_session_id,
            'status': 'running',
            'repo_id': repo_id,
            'model': model_id,
            'message': f'Task submitted. OpenCode is working on: {task_label}',
            'review_url': f'http://localhost:5000/agent/sessions',
        }, indent=2)

    except Exception as e:
        logger.exception('submit_task failed')
        return json.dumps({'error': str(e)})


@mcp.tool()
def list_tasks(repo_id: str = "", status: str = "") -> str:
    """List all submitted tasks, optionally filtered by repo and/or status.

    Status values: running, completed, review, merged, rejected, failed, stalled
    """
    sessions = agent_schema.list_sessions(
        repo_id=repo_id or None,
        status=status or None,
    )

    result = []
    for s in sessions:
        sid = s['session_id']
        # Merge in-memory tracking info
        with _tasks_lock:
            tracked = _active_tasks.get(sid, {})

        effective_status = tracked.get('status') or s.get('status', '')

        result.append({
            'task_id': sid,
            'repo_id': s.get('repo_id', ''),
            'task_description': s.get('task_description', ''),
            'status': effective_status,
            'backend': s.get('backend', ''),
            'created_at': s.get('created_at', ''),
            'completed_at': s.get('completed_at'),
        })

    return json.dumps(result, indent=2)


@mcp.tool()
def get_task_status(task_id: str) -> str:
    """Get the current status of a submitted task.

    Returns status, file change stats, and elapsed time."""
    session = agent_schema.get_session(task_id)
    if not session:
        return json.dumps({'error': f'Task not found: {task_id}'})

    with _tasks_lock:
        tracked = _active_tasks.get(task_id, {})

    files = agent_schema.get_session_files(task_id)
    modified = [f for f in files if f.get('status') in ('modified', 'new')]

    effective_status = tracked.get('status') or session.get('status', '')
    elapsed = None
    if tracked.get('submitted_at'):
        elapsed = round(time.time() - tracked['submitted_at'])

    return json.dumps({
        'task_id': task_id,
        'status': effective_status,
        'task_description': session.get('task_description', ''),
        'repo_id': session.get('repo_id', ''),
        'files_modified': len(modified),
        'total_lines_added': sum(f.get('lines_added', 0) for f in modified),
        'total_lines_removed': sum(f.get('lines_removed', 0) for f in modified),
        'elapsed_seconds': elapsed,
        'error': tracked.get('error'),
    }, indent=2)


@mcp.tool()
def get_task_result(task_id: str) -> str:
    """Get the result of a completed task.

    Returns modified files, diff stats, agent summary, and review URL.
    Only meaningful for tasks with status 'completed' or later."""
    session = agent_schema.get_session(task_id)
    if not session:
        return json.dumps({'error': f'Task not found: {task_id}'})

    files = agent_schema.get_modified_files(task_id)

    # Get agent's final summary from conversation
    summary = ''
    try:
        conversation = agent_schema.get_conversation(task_id)
        for msg in reversed(conversation):
            if msg.get('role') == 'assistant':
                summary = (msg.get('content') or '')[:500]
                break
    except Exception:
        pass

    return json.dumps({
        'task_id': task_id,
        'status': session.get('status', ''),
        'task_description': session.get('task_description', ''),
        'modified_files': [{
            'path': f['relative_path'],
            'lines_added': f.get('lines_added', 0),
            'lines_removed': f.get('lines_removed', 0),
            'status': f.get('status', 'modified'),
        } for f in files],
        'total_files_modified': len(files),
        'total_lines_added': sum(f.get('lines_added', 0) for f in files),
        'total_lines_removed': sum(f.get('lines_removed', 0) for f in files),
        'agent_summary': summary,
        'review_url': f'http://localhost:5000/agent/sessions',
    }, indent=2)


@mcp.tool()
def cancel_task(task_id: str) -> str:
    """Cancel a running task. Aborts the OpenCode session."""
    session = agent_schema.get_session(task_id)
    if not session:
        return json.dumps({'error': f'Task not found: {task_id}'})

    if session['status'] not in ('pending', 'running'):
        return json.dumps({'error': f'Task is not running (status: {session["status"]})'})

    # Try to abort via backend
    try:
        backend_sid = session.get('backend_session_id')
        if backend_sid and session.get('backend') == 'opencode':
            from agent_backends import get_repo_server
            server_info = get_repo_server(session.get('repo_id', ''))
            if server_info:
                from agentic.engine.opencode_client import OpenCodeClient
                client = OpenCodeClient(server_info['base_url'])
                client.abort(backend_sid)
    except Exception as e:
        logger.warning("Abort failed for %s: %s", task_id, e)

    agent_schema.update_session_status(task_id, 'rejected',
                                       error_message='Cancelled by orchestrator')
    _update_task(task_id, status='failed', error='Cancelled')

    return json.dumps({
        'task_id': task_id,
        'status': 'cancelled',
        'message': 'Task cancelled.',
    }, indent=2)


@mcp.tool()
def batch_submit(tasks: str) -> str:
    """Submit multiple tasks at once.

    Args:
        tasks: JSON array of objects, each with 'repo_id' and 'task' keys.
               Optional: 'model', 'agent_type'.

    Example:
        [{"repo_id": "abc123", "task": "Fix login bug"},
         {"repo_id": "abc123", "task": "Add unit tests for auth"}]

    Returns: JSON array of submission results.
    """
    try:
        task_list = json.loads(tasks)
    except (json.JSONDecodeError, TypeError):
        return json.dumps({'error': 'Invalid JSON. Provide a JSON array of task objects.'})

    if not isinstance(task_list, list):
        return json.dumps({'error': 'Expected a JSON array'})

    results = []
    for t in task_list:
        repo_id = t.get('repo_id', '')
        task_desc = t.get('task', '')
        model = t.get('model', '')
        agent_type = t.get('agent_type', 'build')

        result_str = submit_task(repo_id, task_desc, model, agent_type)
        results.append(json.loads(result_str))

    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Guanine Orchestrator MCP Server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"],
                        default="stdio")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    if args.transport == "streamable-http":
        mcp.run(transport="streamable-http", host="127.0.0.1", port=args.port)
    else:
        mcp.run(transport="stdio")
