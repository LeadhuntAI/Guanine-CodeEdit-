"""
Agent session schema and CRUD helpers for the sandboxed coding agent review system.

Global database: sessions/agent_registry.db
Tracks repos, agent sessions, checked-out files, conversations, and review decisions.

Architecture:
    - Thread-local SQLite connections with WAL journaling
    - Deterministic repo IDs via SHA-256 of normalized absolute path
    - Session IDs include timestamp for chronological sorting
    - Foreign keys enforce referential integrity across all tables
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

VALID_SESSION_STATUSES = frozenset({
    'pending', 'running', 'completed', 'review', 'merged', 'rejected', 'error'
})
VALID_REVIEW_DECISIONS = frozenset({'accepted', 'rejected', 'edited', 'reverted'})

# ---------------------------------------------------------------------------
# Database location & connection management
# ---------------------------------------------------------------------------

SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
REGISTRY_DB = os.path.join(SESSIONS_DIR, 'agent_registry.db')

_db_local = threading.local()


def _ensure_sessions_dir():
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def get_agent_db() -> sqlite3.Connection:
    """Get or create a thread-local connection to the global agent registry DB.

    Connections are cached per-thread and reused across calls. If the cached
    connection is broken (e.g., database was deleted), a new one is created.
    WAL mode is enabled for concurrent read access during background writes.
    """
    conn = getattr(_db_local, 'agent_db', None)
    if conn is not None:
        try:
            conn.execute('SELECT 1')
            return conn
        except sqlite3.Error:
            logger.warning("Stale DB connection detected, reconnecting")
            conn = None
    _ensure_sessions_dir()
    conn = sqlite3.connect(REGISTRY_DB, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA cache_size=-8000')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.row_factory = sqlite3.Row
    _db_local.agent_db = conn
    _init_agent_schema(conn)
    return conn


def close_agent_db():
    """Close the thread-local agent registry connection."""
    conn = getattr(_db_local, 'agent_db', None)
    if conn:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _db_local.agent_db = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _init_agent_schema(conn: sqlite3.Connection):
    """Create agent registry tables if they don't exist.

    Tables:
        repos              — Registered repositories with ignore/command config
        agent_sessions     — Agent work sessions with lifecycle timestamps
        session_files      — Files checked out or created during a session
        session_conversation — Full LLM conversation log per session
        review_decisions   — Per-file human review outcomes
    """
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS repos (
            repo_id          TEXT PRIMARY KEY,
            repo_name        TEXT NOT NULL,
            repo_path        TEXT NOT NULL UNIQUE,
            registered_at    TEXT NOT NULL,
            last_scanned_at  TEXT,
            file_count       INTEGER DEFAULT 0,
            ignore_patterns  TEXT DEFAULT '[]',
            allowed_commands TEXT DEFAULT '[]',
            allow_free_commands INTEGER DEFAULT 0,
            settings_json    TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS agent_sessions (
            session_id         TEXT PRIMARY KEY,
            repo_id            TEXT NOT NULL REFERENCES repos(repo_id),
            workspace_path     TEXT NOT NULL,
            task_description   TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'pending',
            agent_model        TEXT,
            external_context   TEXT,
            created_at         TEXT NOT NULL,
            started_at         TEXT,
            completed_at       TEXT,
            reviewed_at        TEXT,
            merged_at          TEXT,
            merge_session_id   TEXT,
            workflow_json      TEXT,
            error_message      TEXT,
            backend            TEXT DEFAULT 'builtin',
            backend_session_id TEXT,
            parent_session_id  TEXT
        );

        CREATE TABLE IF NOT EXISTS session_files (
            session_id       TEXT NOT NULL REFERENCES agent_sessions(session_id),
            relative_path    TEXT NOT NULL,
            checkout_hash    TEXT NOT NULL,
            current_hash     TEXT,
            lines_added      INTEGER DEFAULT 0,
            lines_removed    INTEGER DEFAULT 0,
            status           TEXT DEFAULT 'checked_out',
            checked_out_at   TEXT NOT NULL,
            last_modified_at TEXT,
            PRIMARY KEY (session_id, relative_path)
        );

        CREATE TABLE IF NOT EXISTS session_conversation (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       TEXT NOT NULL REFERENCES agent_sessions(session_id),
            role             TEXT NOT NULL,
            content          TEXT,
            tool_call_id     TEXT,
            tool_calls       TEXT,
            timestamp        TEXT NOT NULL,
            layer_index      INTEGER
        );

        CREATE TABLE IF NOT EXISTS review_decisions (
            session_id       TEXT NOT NULL REFERENCES agent_sessions(session_id),
            relative_path    TEXT NOT NULL,
            decision         TEXT NOT NULL,
            reviewer_notes   TEXT,
            decided_at       TEXT NOT NULL,
            PRIMARY KEY (session_id, relative_path)
        );

        CREATE INDEX IF NOT EXISTS idx_as_repo ON agent_sessions(repo_id);
        CREATE INDEX IF NOT EXISTS idx_as_status ON agent_sessions(status);
        CREATE INDEX IF NOT EXISTS idx_sf_session ON session_files(session_id);
        CREATE INDEX IF NOT EXISTS idx_sc_session ON session_conversation(session_id);
        CREATE INDEX IF NOT EXISTS idx_rd_session ON review_decisions(session_id);
    ''')

    # Backwards-compatible migration: add new columns to existing databases
    for col, default in [('backend', "'builtin'"),
                         ('backend_session_id', 'NULL'),
                         ('parent_session_id', 'NULL')]:
        try:
            conn.execute(f'ALTER TABLE agent_sessions ADD COLUMN {col} TEXT DEFAULT {default}')
        except Exception:
            pass  # Column already exists

    # Repos: add settings_json column
    try:
        conn.execute("ALTER TABLE repos ADD COLUMN settings_json TEXT DEFAULT '{}'")
    except Exception:
        pass

    # Index on parent_session_id — must come after migration adds the column
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS idx_as_parent ON agent_sessions(parent_session_id)')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _repo_id(repo_path: str) -> str:
    """Deterministic repo ID from absolute path.

    Uses first 16 hex chars of SHA-256 of the normalized absolute path.
    This ensures the same directory always gets the same ID regardless
    of how the path is specified (relative, trailing slash, etc.).
    """
    normalized = os.path.normpath(os.path.abspath(repo_path))
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def _generate_session_id() -> str:
    """Generate a unique session ID with embedded timestamp."""
    return f"agent_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _workspace_path(session_id: str) -> str:
    """Return the workspace directory path for a session."""
    return os.path.join(SESSIONS_DIR, session_id, 'workspace')


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def _deserialize_repo(d: dict) -> dict:
    """Deserialize JSON fields in a repo dict."""
    d['ignore_patterns'] = json.loads(d.get('ignore_patterns', '[]'))
    d['allowed_commands'] = json.loads(d.get('allowed_commands', '[]'))
    d['allow_free_commands'] = bool(d.get('allow_free_commands', 0))
    try:
        d['settings'] = json.loads(d.get('settings_json', '{}') or '{}')
    except (json.JSONDecodeError, TypeError):
        d['settings'] = {}
    return d


# ---------------------------------------------------------------------------
# Repo CRUD
# ---------------------------------------------------------------------------

def register_repo(repo_path: str, repo_name: str,
                  ignore_patterns: Optional[list] = None,
                  allowed_commands: Optional[list] = None,
                  allow_free_commands: bool = False) -> dict:
    """Register a repository for agent sessions.

    Args:
        repo_path: Absolute or relative path to the repository directory.
        repo_name: Human-readable name for the repository.
        ignore_patterns: List of glob patterns to ignore during file listing.
        allowed_commands: List of allowed command prefixes for run_command.
        allow_free_commands: If True, bypass the command whitelist entirely.

    Returns:
        The registered repo dict.

    Raises:
        ValueError: If the directory does not exist.
    """
    db = get_agent_db()
    abs_path = os.path.normpath(os.path.abspath(repo_path))
    if not os.path.isdir(abs_path):
        raise ValueError(f"Directory does not exist: {abs_path}")

    rid = _repo_id(abs_path)
    now = _now_iso()
    ignore = json.dumps(ignore_patterns or [])
    allowed = json.dumps(allowed_commands or [])

    db.execute('''
        INSERT INTO repos (repo_id, repo_name, repo_path, registered_at,
                          ignore_patterns, allowed_commands, allow_free_commands)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id) DO UPDATE SET
            repo_name = excluded.repo_name,
            ignore_patterns = excluded.ignore_patterns,
            allowed_commands = excluded.allowed_commands,
            allow_free_commands = excluded.allow_free_commands
    ''', (rid, repo_name, abs_path, now, ignore, allowed,
          1 if allow_free_commands else 0))
    db.commit()
    logger.info("Registered repo %s (%s) at %s", repo_name, rid, abs_path)
    return get_repo(rid)


def get_repo(repo_id: str) -> Optional[dict]:
    """Get a repo by ID. Returns None if not found."""
    db = get_agent_db()
    row = db.execute('SELECT * FROM repos WHERE repo_id = ?', (repo_id,)).fetchone()
    if row is None:
        return None
    return _deserialize_repo(_row_to_dict(row))


def list_repos() -> list[dict]:
    """List all registered repos, ordered by registration date (newest first)."""
    db = get_agent_db()
    rows = db.execute('SELECT * FROM repos ORDER BY registered_at DESC').fetchall()
    return [_deserialize_repo(_row_to_dict(row)) for row in rows]


def update_repo(repo_id: str, **kwargs) -> Optional[dict]:
    """Update repo fields. Supports: repo_name, ignore_patterns, allowed_commands,
    allow_free_commands, last_scanned_at, file_count."""
    db = get_agent_db()
    allowed_fields = {'repo_name', 'ignore_patterns', 'allowed_commands',
                      'allow_free_commands', 'last_scanned_at', 'file_count',
                      'settings_json'}
    updates = []
    values = []
    for key, val in kwargs.items():
        if key not in allowed_fields:
            logger.warning("Ignoring unknown repo field: %s", key)
            continue
        if key in ('ignore_patterns', 'allowed_commands'):
            val = json.dumps(val)
        elif key == 'allow_free_commands':
            val = 1 if val else 0
        updates.append(f'{key} = ?')
        values.append(val)
    if not updates:
        return get_repo(repo_id)
    values.append(repo_id)
    db.execute(f'UPDATE repos SET {", ".join(updates)} WHERE repo_id = ?', values)
    db.commit()
    return get_repo(repo_id)


def delete_repo(repo_id: str) -> bool:
    """Delete a repo and cascade-delete all its sessions and child data."""
    db = get_agent_db()
    sessions = db.execute(
        'SELECT session_id FROM agent_sessions WHERE repo_id = ?', (repo_id,)
    ).fetchall()
    for s in sessions:
        _delete_session_data(db, s['session_id'])
    db.execute('DELETE FROM agent_sessions WHERE repo_id = ?', (repo_id,))
    db.execute('DELETE FROM repos WHERE repo_id = ?', (repo_id,))
    db.commit()
    logger.info("Deleted repo %s and %d sessions", repo_id, len(sessions))
    return True


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

def create_session(repo_id: str, task_description: str,
                   agent_model: Optional[str] = None,
                   external_context: Optional[str] = None,
                   workflow_json: Optional[dict] = None,
                   backend: Optional[str] = None,
                   backend_session_id: Optional[str] = None,
                   parent_session_id: Optional[str] = None) -> dict:
    """Create a new agent session with its workspace directory.

    Args:
        repo_id: ID of the registered repository.
        task_description: Human-readable description of the agent's task.
        agent_model: Optional model identifier (e.g., 'gpt-4o').
        external_context: Optional context string passed from external systems.
        workflow_json: Optional workflow definition dict.
        backend: Agent backend name ('builtin', 'opencode', etc.). Defaults to 'builtin'.
        backend_session_id: Backend-specific session reference (e.g., OpenCode session ID).
        parent_session_id: ID of the parent session for orchestrator-spawned sub-sessions.

    Returns:
        The created session dict.

    Raises:
        ValueError: If the repo is not found.
    """
    db = get_agent_db()
    repo = get_repo(repo_id)
    if repo is None:
        raise ValueError(f"Repo not found: {repo_id}")

    sid = _generate_session_id()
    ws = _workspace_path(sid)
    os.makedirs(ws, exist_ok=True)
    now = _now_iso()

    db.execute('''
        INSERT INTO agent_sessions
        (session_id, repo_id, workspace_path, task_description, status,
         agent_model, external_context, created_at, workflow_json,
         backend, backend_session_id, parent_session_id)
        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
    ''', (sid, repo_id, ws, task_description,
          agent_model, external_context, now,
          json.dumps(workflow_json) if workflow_json else None,
          backend or 'builtin', backend_session_id, parent_session_id))
    db.commit()
    logger.info("Created session %s for repo %s (backend=%s)", sid, repo_id, backend or 'builtin')
    return get_session(sid)


def get_session(session_id: str) -> Optional[dict]:
    """Get a session by ID, including aggregated file stats.

    The returned dict includes a 'file_stats' key with per-status counts
    of files and total lines added/removed.
    """
    db = get_agent_db()
    row = db.execute(
        'SELECT * FROM agent_sessions WHERE session_id = ?', (session_id,)
    ).fetchone()
    if row is None:
        return None
    d = _row_to_dict(row)
    if d['workflow_json']:
        try:
            d['workflow_json'] = json.loads(d['workflow_json'])
        except (json.JSONDecodeError, TypeError):
            pass

    # Attach file summary
    files = db.execute('''
        SELECT status, COUNT(*) as cnt,
               COALESCE(SUM(lines_added), 0) as total_added,
               COALESCE(SUM(lines_removed), 0) as total_removed
        FROM session_files WHERE session_id = ?
        GROUP BY status
    ''', (session_id,)).fetchall()
    d['file_stats'] = {r['status']: {
        'count': r['cnt'],
        'lines_added': r['total_added'],
        'lines_removed': r['total_removed']
    } for r in files}
    return d


def list_sessions(repo_id: Optional[str] = None,
                  status: Optional[str] = None) -> list[dict]:
    """List agent sessions with optional filters.

    Args:
        repo_id: Filter by repository ID.
        status: Filter by session status.

    Returns:
        List of session dicts ordered by creation date (newest first).
    """
    db = get_agent_db()
    query = 'SELECT * FROM agent_sessions WHERE 1=1'
    params = []
    if repo_id:
        query += ' AND repo_id = ?'
        params.append(repo_id)
    if status:
        if status not in VALID_SESSION_STATUSES:
            logger.warning("Filtering by unknown status: %s", status)
        query += ' AND status = ?'
        params.append(status)
    query += ' ORDER BY created_at DESC'
    rows = db.execute(query, params).fetchall()
    result = []
    for row in rows:
        d = _row_to_dict(row)
        if d['workflow_json']:
            try:
                d['workflow_json'] = json.loads(d['workflow_json'])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(d)
    return result


def get_child_sessions(parent_session_id: str) -> list[dict]:
    """Get all sub-sessions spawned by a parent session.

    Returns:
        List of session dicts ordered by creation date (oldest first).
    """
    db = get_agent_db()
    rows = db.execute(
        'SELECT * FROM agent_sessions WHERE parent_session_id = ? ORDER BY created_at ASC',
        (parent_session_id,)
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def update_session_status(session_id: str, status: str,
                          error_message: Optional[str] = None) -> Optional[dict]:
    """Update session status and set the corresponding lifecycle timestamp.

    Automatically sets started_at, completed_at, reviewed_at, or merged_at
    based on the new status value.
    """
    db = get_agent_db()
    if status not in VALID_SESSION_STATUSES:
        raise ValueError(f"Invalid status: {status}. Must be one of {VALID_SESSION_STATUSES}")

    now = _now_iso()
    timestamp_map = {
        'running': 'started_at',
        'completed': 'completed_at',
        'review': 'reviewed_at',
        'merged': 'merged_at',
    }

    updates = ['status = ?']
    values = [status]

    ts_col = timestamp_map.get(status)
    if ts_col:
        updates.append(f'{ts_col} = ?')
        values.append(now)

    if error_message is not None:
        updates.append('error_message = ?')
        values.append(error_message)

    values.append(session_id)
    db.execute(
        f'UPDATE agent_sessions SET {", ".join(updates)} WHERE session_id = ?',
        values
    )
    db.commit()
    logger.info("Session %s status -> %s", session_id, status)
    return get_session(session_id)


def rename_session(session_id: str, task_description: str) -> Optional[dict]:
    """Rename a session's task description."""
    db = get_agent_db()
    db.execute(
        'UPDATE agent_sessions SET task_description = ? WHERE session_id = ?',
        (task_description, session_id)
    )
    db.commit()
    return get_session(session_id)


def set_merge_session_id(session_id: str, merge_session_id: str):
    """Link an agent session to its file_merger review session."""
    db = get_agent_db()
    db.execute(
        'UPDATE agent_sessions SET merge_session_id = ? WHERE session_id = ?',
        (merge_session_id, session_id)
    )
    db.commit()


def _delete_session_data(db: sqlite3.Connection, session_id: str):
    """Delete all child data for a session (files, conversation, decisions)."""
    db.execute('DELETE FROM review_decisions WHERE session_id = ?', (session_id,))
    db.execute('DELETE FROM session_conversation WHERE session_id = ?', (session_id,))
    db.execute('DELETE FROM session_files WHERE session_id = ?', (session_id,))


def delete_session(session_id: str) -> bool:
    """Delete an agent session, its child data, and its workspace directory."""
    db = get_agent_db()
    session = get_session(session_id)
    if session is None:
        return False

    _delete_session_data(db, session_id)
    db.execute('DELETE FROM agent_sessions WHERE session_id = ?', (session_id,))
    db.commit()

    # Clean up workspace directory
    import shutil
    session_dir = os.path.join(SESSIONS_DIR, session_id)
    if os.path.isdir(session_dir):
        shutil.rmtree(session_dir, ignore_errors=True)
    logger.info("Deleted session %s", session_id)
    return True


# ---------------------------------------------------------------------------
# Session Files
# ---------------------------------------------------------------------------

def record_file_checkout(session_id: str, relative_path: str,
                         checkout_hash: str) -> dict:
    """Record that a file was checked out into the workspace.

    Uses INSERT ... ON CONFLICT to handle re-checkouts of the same file,
    resetting stats to their initial values.
    """
    db = get_agent_db()
    now = _now_iso()
    db.execute('''
        INSERT INTO session_files (session_id, relative_path, checkout_hash,
                                   status, checked_out_at)
        VALUES (?, ?, ?, 'checked_out', ?)
        ON CONFLICT(session_id, relative_path) DO UPDATE SET
            checkout_hash = excluded.checkout_hash,
            checked_out_at = excluded.checked_out_at,
            status = 'checked_out',
            current_hash = NULL,
            lines_added = 0,
            lines_removed = 0
    ''', (session_id, relative_path, checkout_hash, now))
    db.commit()
    return {'session_id': session_id, 'relative_path': relative_path,
            'checkout_hash': checkout_hash, 'status': 'checked_out'}


def update_file_stats(session_id: str, relative_path: str,
                      current_hash: str, lines_added: int,
                      lines_removed: int, status: str = 'modified'):
    """Update file stats after an agent edit.

    Args:
        session_id: The session owning the file.
        relative_path: Path relative to workspace root.
        current_hash: SHA-256 hash of the current file content.
        lines_added: Number of lines added compared to original.
        lines_removed: Number of lines removed compared to original.
        status: New file status (default: 'modified').
    """
    db = get_agent_db()
    now = _now_iso()
    db.execute('''
        UPDATE session_files
        SET current_hash = ?, lines_added = ?, lines_removed = ?,
            status = ?, last_modified_at = ?
        WHERE session_id = ? AND relative_path = ?
    ''', (current_hash, lines_added, lines_removed, status, now,
          session_id, relative_path))
    db.commit()


def record_new_file(session_id: str, relative_path: str,
                    current_hash: str, line_count: int):
    """Record a new file created by the agent (not checked out from repo)."""
    db = get_agent_db()
    now = _now_iso()
    db.execute('''
        INSERT INTO session_files (session_id, relative_path, checkout_hash,
                                   current_hash, lines_added, lines_removed,
                                   status, checked_out_at, last_modified_at)
        VALUES (?, ?, '', ?, ?, 0, 'new', ?, ?)
        ON CONFLICT(session_id, relative_path) DO UPDATE SET
            current_hash = excluded.current_hash,
            lines_added = excluded.lines_added,
            status = 'new',
            last_modified_at = excluded.last_modified_at
    ''', (session_id, relative_path, current_hash, line_count, now, now))
    db.commit()


def get_session_files(session_id: str,
                      status: Optional[str] = None) -> list[dict]:
    """List files in an agent session, optionally filtered by status."""
    db = get_agent_db()
    query = 'SELECT * FROM session_files WHERE session_id = ?'
    params = [session_id]
    if status:
        query += ' AND status = ?'
        params.append(status)
    query += ' ORDER BY relative_path'
    rows = db.execute(query, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_modified_files(session_id: str) -> list[dict]:
    """Get files that were actually changed (modified or new)."""
    db = get_agent_db()
    rows = db.execute('''
        SELECT * FROM session_files
        WHERE session_id = ? AND status IN ('modified', 'new')
        ORDER BY relative_path
    ''', (session_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

def save_conversation_message(session_id: str, role: str, content: Optional[str],
                              tool_call_id: Optional[str] = None,
                              tool_calls: Optional[list] = None,
                              layer_index: Optional[int] = None):
    """Append a conversation message to a session's history."""
    db = get_agent_db()
    now = _now_iso()
    db.execute('''
        INSERT INTO session_conversation
        (session_id, role, content, tool_call_id, tool_calls, timestamp, layer_index)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (session_id, role, content, tool_call_id,
          json.dumps(tool_calls) if tool_calls else None,
          now, layer_index))
    db.commit()


def get_conversation(session_id: str) -> list[dict]:
    """Get the full ordered conversation history for a session."""
    db = get_agent_db()
    rows = db.execute('''
        SELECT * FROM session_conversation
        WHERE session_id = ? ORDER BY id
    ''', (session_id,)).fetchall()
    result = []
    for row in rows:
        d = _row_to_dict(row)
        if d['tool_calls']:
            try:
                d['tool_calls'] = json.loads(d['tool_calls'])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Review Decisions
# ---------------------------------------------------------------------------

def record_review_decision(session_id: str, relative_path: str,
                           decision: str,
                           reviewer_notes: Optional[str] = None):
    """Record or update a review decision for a file.

    Uses UPSERT semantics — calling this again for the same file
    overwrites the previous decision.
    """
    db = get_agent_db()
    if decision not in VALID_REVIEW_DECISIONS:
        raise ValueError(f"Invalid decision: {decision}. Must be one of {VALID_REVIEW_DECISIONS}")
    now = _now_iso()
    db.execute('''
        INSERT INTO review_decisions (session_id, relative_path, decision,
                                      reviewer_notes, decided_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id, relative_path) DO UPDATE SET
            decision = excluded.decision,
            reviewer_notes = excluded.reviewer_notes,
            decided_at = excluded.decided_at
    ''', (session_id, relative_path, decision, reviewer_notes, now))
    db.commit()


def get_review_decisions(session_id: str) -> list[dict]:
    """Get all review decisions for a session."""
    db = get_agent_db()
    rows = db.execute('''
        SELECT * FROM review_decisions
        WHERE session_id = ? ORDER BY relative_path
    ''', (session_id,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_review_summary(session_id: str) -> dict:
    """Get a summary of review progress for a session.

    Returns:
        Dict with keys: total_files, reviewed, accepted, rejected, edited, pending.
    """
    db = get_agent_db()
    total_modified = db.execute('''
        SELECT COUNT(*) FROM session_files
        WHERE session_id = ? AND status IN ('modified', 'new')
    ''', (session_id,)).fetchone()[0]

    decisions = db.execute('''
        SELECT decision, COUNT(*) as cnt
        FROM review_decisions WHERE session_id = ?
        GROUP BY decision
    ''', (session_id,)).fetchall()

    summary = {
        'total_files': total_modified,
        'reviewed': sum(r['cnt'] for r in decisions),
        'accepted': 0,
        'rejected': 0,
        'edited': 0,
    }
    for r in decisions:
        summary[r['decision']] = r['cnt']
    summary['pending'] = summary['total_files'] - summary['reviewed']
    return summary