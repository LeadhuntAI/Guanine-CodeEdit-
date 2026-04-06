"""
Agent session schema and CRUD helpers for the sandboxed coding agent review system.

Global database: sessions/agent_registry.db
Tracks repos, agent sessions, checked-out files, conversations, and review decisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Database location & connection management
# ---------------------------------------------------------------------------

SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
REGISTRY_DB = os.path.join(SESSIONS_DIR, 'agent_registry.db')

_db_local = threading.local()


def _ensure_sessions_dir():
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def get_agent_db() -> sqlite3.Connection:
    """Get or create a thread-local connection to the global agent registry DB."""
    conn = getattr(_db_local, 'agent_db', None)
    if conn is not None:
        try:
            conn.execute('SELECT 1')
            return conn
        except sqlite3.Error:
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
    """Create agent registry tables if they don't exist."""
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
            allow_free_commands INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS agent_sessions (
            session_id       TEXT PRIMARY KEY,
            repo_id          TEXT NOT NULL REFERENCES repos(repo_id),
            workspace_path   TEXT NOT NULL,
            task_description TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'pending',
            agent_model      TEXT,
            external_context TEXT,
            created_at       TEXT NOT NULL,
            started_at       TEXT,
            completed_at     TEXT,
            reviewed_at      TEXT,
            merged_at        TEXT,
            merge_session_id TEXT,
            workflow_json    TEXT,
            error_message    TEXT
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
    ''')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat()


def _repo_id(repo_path: str) -> str:
    """Deterministic repo ID from absolute path."""
    normalized = os.path.normpath(os.path.abspath(repo_path))
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def _generate_session_id() -> str:
    return f"agent_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _workspace_path(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, session_id, 'workspace')


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


# ---------------------------------------------------------------------------
# Repo CRUD
# ---------------------------------------------------------------------------

def register_repo(repo_path: str, repo_name: str,
                  ignore_patterns: Optional[list] = None,
                  allowed_commands: Optional[list] = None,
                  allow_free_commands: bool = False) -> dict:
    """Register a repository for agent sessions. Returns repo dict."""
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
    return get_repo(rid)


def get_repo(repo_id: str) -> Optional[dict]:
    """Get a repo by ID."""
    db = get_agent_db()
    row = db.execute('SELECT * FROM repos WHERE repo_id = ?', (repo_id,)).fetchone()
    if row is None:
        return None
    d = _row_to_dict(row)
    d['ignore_patterns'] = json.loads(d['ignore_patterns'])
    d['allowed_commands'] = json.loads(d['allowed_commands'])
    d['allow_free_commands'] = bool(d['allow_free_commands'])
    return d


def list_repos() -> list[dict]:
    """List all registered repos."""
    db = get_agent_db()
    rows = db.execute('SELECT * FROM repos ORDER BY registered_at DESC').fetchall()
    result = []
    for row in rows:
        d = _row_to_dict(row)
        d['ignore_patterns'] = json.loads(d['ignore_patterns'])
        d['allowed_commands'] = json.loads(d['allowed_commands'])
        d['allow_free_commands'] = bool(d['allow_free_commands'])
        result.append(d)
    return result


def update_repo(repo_id: str, **kwargs) -> Optional[dict]:
    """Update repo fields. Supports: repo_name, ignore_patterns, allowed_commands,
    allow_free_commands, last_scanned_at, file_count."""
    db = get_agent_db()
    allowed_fields = {'repo_name', 'ignore_patterns', 'allowed_commands',
                      'allow_free_commands', 'last_scanned_at', 'file_count'}
    updates = []
    values = []
    for key, val in kwargs.items():
        if key not in allowed_fields:
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
    """Delete a repo and all its sessions."""
    db = get_agent_db()
    # Delete in dependency order
    sessions = db.execute(
        'SELECT session_id FROM agent_sessions WHERE repo_id = ?', (repo_id,)
    ).fetchall()
    for s in sessions:
        _delete_session_data(db, s['session_id'])
    db.execute('DELETE FROM agent_sessions WHERE repo_id = ?', (repo_id,))
    db.execute('DELETE FROM repos WHERE repo_id = ?', (repo_id,))
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

def create_session(repo_id: str, task_description: str,
                   agent_model: Optional[str] = None,
                   external_context: Optional[str] = None,
                   workflow_json: Optional[dict] = None) -> dict:
    """Create a new agent session with its workspace directory."""
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
         agent_model, external_context, created_at, workflow_json)
        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
    ''', (sid, repo_id, ws, task_description,
          agent_model, external_context, now,
          json.dumps(workflow_json) if workflow_json else None))
    db.commit()
    return get_session(sid)


def get_session(session_id: str) -> Optional[dict]:
    """Get a session by ID, including file stats."""
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
               SUM(lines_added) as total_added,
               SUM(lines_removed) as total_removed
        FROM session_files WHERE session_id = ?
        GROUP BY status
    ''', (session_id,)).fetchall()
    d['file_stats'] = {r['status']: {
        'count': r['cnt'],
        'lines_added': r['total_added'] or 0,
        'lines_removed': r['total_removed'] or 0
    } for r in files}
    return d


def list_sessions(repo_id: Optional[str] = None,
                  status: Optional[str] = None) -> list[dict]:
    """List agent sessions with optional filters."""
    db = get_agent_db()
    query = 'SELECT * FROM agent_sessions WHERE 1=1'
    params = []
    if repo_id:
        query += ' AND repo_id = ?'
        params.append(repo_id)
    if status:
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


def update_session_status(session_id: str, status: str,
                          error_message: Optional[str] = None) -> Optional[dict]:
    """Update session status and corresponding timestamp."""
    db = get_agent_db()
    valid = {'pending', 'running', 'completed', 'review', 'merged', 'rejected', 'error'}
    if status not in valid:
        raise ValueError(f"Invalid status: {status}. Must be one of {valid}")

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
    """Delete an agent session and its workspace."""
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
    return True


# ---------------------------------------------------------------------------
# Session Files
# ---------------------------------------------------------------------------

def record_file_checkout(session_id: str, relative_path: str,
                         checkout_hash: str) -> dict:
    """Record that a file was checked out into the workspace."""
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
    """Update file stats after an agent edit."""
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
    ''', (session_id, relative_path, current_hash, line_count, now, now))
    db.commit()


def get_session_files(session_id: str,
                      status: Optional[str] = None) -> list[dict]:
    """List files in an agent session."""
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
    """Save a conversation message for an agent session."""
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
    """Get the full conversation history for a session."""
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
    """Record a review decision for a file."""
    db = get_agent_db()
    valid = {'accepted', 'rejected', 'edited'}
    if decision not in valid:
        raise ValueError(f"Invalid decision: {decision}. Must be one of {valid}")
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
    """Get a summary of review progress for a session."""
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
