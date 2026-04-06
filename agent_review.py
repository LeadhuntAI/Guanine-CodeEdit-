"""
Flask Blueprint for the sandboxed coding agent review system.

Provides routes for:
- Repo registration and management
- Agent session creation, listing, and detail
- Review bridge: creates file_merger sessions from agent workspaces
- Merge-back: copies accepted changes to the original repo
- Conversation viewing and continuation
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import time
from datetime import datetime
from typing import Optional

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify,
)

import agent_schema
import agent_tools
import agent_workflow

# We import from file_merger at function-call time to avoid circular imports
# at module load. See _get_file_merger() helper below.

agent_bp = Blueprint('agent', __name__, template_folder='templates')


# ---------------------------------------------------------------------------
# Lazy import helper (avoids circular import with file_merger.py)
# ---------------------------------------------------------------------------

_file_merger = None


def _get_file_merger():
    global _file_merger
    if _file_merger is None:
        import file_merger as fm
        _file_merger = fm
    return _file_merger


# ---------------------------------------------------------------------------
# Repo routes
# ---------------------------------------------------------------------------

@agent_bp.route('/repos', methods=['GET'])
def repos():
    """List registered repos."""
    all_repos = agent_schema.list_repos()
    return render_template('agent_repos.html', repos=all_repos)


@agent_bp.route('/repos', methods=['POST'])
def register_repo():
    """Register a new repo."""
    repo_path = request.form.get('repo_path', '').strip()
    repo_name = request.form.get('repo_name', '').strip()
    allowed_cmds = request.form.get('allowed_commands', '').strip()
    allow_free = request.form.get('allow_free_commands') == 'on'

    if not repo_path:
        flash('Repository path is required.', 'error')
        return redirect(url_for('agent.repos'))

    if not repo_name:
        repo_name = os.path.basename(repo_path)

    # Parse allowed commands (one per line)
    cmds = [c.strip() for c in allowed_cmds.splitlines() if c.strip()] if allowed_cmds else []

    try:
        repo = agent_schema.register_repo(
            repo_path=repo_path,
            repo_name=repo_name,
            allowed_commands=cmds,
            allow_free_commands=allow_free,
        )
        flash(f'Registered repo: {repo["repo_name"]}', 'success')
    except ValueError as e:
        flash(str(e), 'error')

    return redirect(url_for('agent.repos'))


@agent_bp.route('/repos/<repo_id>/settings', methods=['GET', 'POST'])
def repo_settings(repo_id):
    """View/edit repo settings."""
    repo = agent_schema.get_repo(repo_id)
    if repo is None:
        flash('Repo not found.', 'error')
        return redirect(url_for('agent.repos'))

    if request.method == 'POST':
        repo_name = request.form.get('repo_name', repo['repo_name']).strip()
        allowed_cmds = request.form.get('allowed_commands', '').strip()
        allow_free = request.form.get('allow_free_commands') == 'on'
        ignore = request.form.get('ignore_patterns', '').strip()

        cmds = [c.strip() for c in allowed_cmds.splitlines() if c.strip()] if allowed_cmds else []
        patterns = [p.strip() for p in ignore.splitlines() if p.strip()] if ignore else []

        agent_schema.update_repo(
            repo_id,
            repo_name=repo_name,
            allowed_commands=cmds,
            allow_free_commands=allow_free,
            ignore_patterns=patterns,
        )
        flash('Settings updated.', 'success')
        return redirect(url_for('agent.repo_settings', repo_id=repo_id))

    return render_template('agent_repos.html', repos=[repo], editing=repo)


@agent_bp.route('/repos/<repo_id>/delete', methods=['POST'])
def delete_repo(repo_id):
    """Delete a registered repo."""
    agent_schema.delete_repo(repo_id)
    flash('Repo deleted.', 'success')
    return redirect(url_for('agent.repos'))


# ---------------------------------------------------------------------------
# Session routes
# ---------------------------------------------------------------------------

@agent_bp.route('/sessions', methods=['GET'])
def sessions():
    """List all agent sessions."""
    repo_filter = request.args.get('repo_id')
    status_filter = request.args.get('status')
    all_sessions = agent_schema.list_sessions(
        repo_id=repo_filter, status=status_filter
    )
    all_repos = agent_schema.list_repos()
    repos_map = {r['repo_id']: r for r in all_repos}
    return render_template('agent_sessions.html',
                           sessions=all_sessions,
                           repos=all_repos,
                           repos_map=repos_map,
                           status_filter=status_filter,
                           repo_filter=repo_filter)


@agent_bp.route('/sessions', methods=['POST'])
def create_session():
    """Create a new agent session."""
    repo_id = request.form.get('repo_id', '').strip()
    task = request.form.get('task_description', '').strip()
    model = request.form.get('agent_model', '').strip() or None
    context = request.form.get('external_context', '').strip() or None

    if not repo_id or not task:
        flash('Repo and task description are required.', 'error')
        return redirect(url_for('agent.sessions'))

    try:
        session = agent_schema.create_session(
            repo_id=repo_id,
            task_description=task,
            agent_model=model,
            external_context=context,
        )
        flash(f'Session created: {session["session_id"]}', 'success')
        return redirect(url_for('agent.session_detail',
                                session_id=session['session_id']))
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(url_for('agent.sessions'))


@agent_bp.route('/session/<session_id>')
def session_detail(session_id):
    """Session detail: status, files, diff summary, actions."""
    session = agent_schema.get_session(session_id)
    if session is None:
        flash('Session not found.', 'error')
        return redirect(url_for('agent.sessions'))

    repo = agent_schema.get_repo(session['repo_id'])
    files = agent_schema.get_session_files(session_id)
    review_summary = agent_schema.get_review_summary(session_id)

    return render_template('agent_session_detail.html',
                           session=session,
                           repo=repo,
                           files=files,
                           review_summary=review_summary)


@agent_bp.route('/session/<session_id>/delete', methods=['POST'])
def delete_session(session_id):
    """Delete an agent session."""
    agent_schema.delete_session(session_id)
    flash('Session deleted.', 'success')
    return redirect(url_for('agent.sessions'))


# ---------------------------------------------------------------------------
# Review Bridge — the critical piece
# ---------------------------------------------------------------------------

def _create_review_session(agent_session_id: str) -> str:
    """Create a file_merger session from an agent workspace.

    For each modified/new file, creates a MergeItem with two FileVersions:
    - Original (from repo)
    - Agent (from workspace)

    Returns the merge session_id.
    """
    fm = _get_file_merger()

    session = agent_schema.get_session(agent_session_id)
    if session is None:
        raise ValueError(f"Agent session not found: {agent_session_id}")

    repo = agent_schema.get_repo(session['repo_id'])
    if repo is None:
        raise ValueError("Associated repo not found")

    modified_files = agent_schema.get_modified_files(agent_session_id)
    if not modified_files:
        raise ValueError("No modified files to review")

    # Generate a merge session ID
    merge_sid = f"review_{agent_session_id}"
    merge_dir = os.path.join(fm.SESSIONS_DIR, merge_sid)
    os.makedirs(merge_dir, exist_ok=True)

    # Build inventory of MergeItems
    inventory = {}
    ws = session['workspace_path']
    repo_path = repo['repo_path']

    for f in modified_files:
        rel = f['relative_path']
        ws_file = os.path.join(ws, rel)
        repo_file = os.path.join(repo_path, rel)

        versions = []

        # Version 0: Original (from repo) — if file exists
        if os.path.isfile(repo_file):
            stat = os.stat(repo_file)
            orig_hash = agent_tools._compute_hash(repo_file)
            try:
                with open(repo_file, 'r', encoding='utf-8', errors='replace') as fh:
                    orig_lines = sum(1 for _ in fh)
            except Exception:
                orig_lines = None
            is_binary = fm.FileScanner.detect_binary(repo_file)

            versions.append(fm.FileVersion(
                source_name='Original',
                source_root=repo_path,
                absolute_path=repo_file,
                relative_path=rel,
                file_size=stat.st_size,
                modified_time=stat.st_mtime,
                created_time=stat.st_ctime,
                sha256=orig_hash,
                line_count=orig_lines if not is_binary else None,
                is_binary=is_binary,
            ))

        # Version 1 (or 0 for new files): Agent's version
        if os.path.isfile(ws_file):
            stat = os.stat(ws_file)
            agent_hash = agent_tools._compute_hash(ws_file)
            try:
                with open(ws_file, 'r', encoding='utf-8', errors='replace') as fh:
                    agent_lines = sum(1 for _ in fh)
            except Exception:
                agent_lines = None
            is_binary = fm.FileScanner.detect_binary(ws_file)

            task_short = session['task_description'][:40]
            versions.append(fm.FileVersion(
                source_name=f'Agent: {task_short}',
                source_root=ws,
                absolute_path=ws_file,
                relative_path=rel,
                file_size=stat.st_size,
                modified_time=stat.st_mtime,
                created_time=stat.st_ctime,
                sha256=agent_hash,
                line_count=agent_lines if not is_binary else None,
                is_binary=is_binary,
            ))

        if not versions:
            continue

        # Determine category
        if f['status'] == 'new':
            category = 'auto_unique'  # New file, just needs accept/reject
        elif len(versions) == 2 and versions[0].sha256 == versions[1].sha256:
            category = 'auto_identical'  # No actual change
        else:
            category = 'conflict'  # Needs review

        # For conflicts, default to agent version (index 1) so "accept all" = accept agent changes
        selected_index = len(versions) - 1 if category == 'conflict' else 0

        item = fm.MergeItem(
            relative_path=rel,
            versions=versions,
            category=category,
            selected_index=selected_index,
            resolved=False,
        )
        inventory[rel] = item

    # Save to the merge session's SQLite DB
    # We need to temporarily point file_merger's state at this session
    old_sid = fm.state.get('_session_id', '')
    old_inv = fm.state.get('inventory')

    fm.state['_session_id'] = merge_sid
    fm.state['inventory'] = inventory
    fm.save_inventory_state(inventory)

    # Save session meta
    fm.save_session_meta(merge_sid,
                         f'Review: {session["task_description"][:60]}')

    # Save config pointing at repo as target
    fm.save_config()

    # Restore old state
    fm.state['_session_id'] = old_sid
    if old_inv is not None:
        fm.state['inventory'] = old_inv

    # Link the merge session to the agent session
    agent_schema.set_merge_session_id(agent_session_id, merge_sid)
    agent_schema.update_session_status(agent_session_id, 'review')

    return merge_sid


@agent_bp.route('/session/<session_id>/review')
def review_session(session_id):
    """Create or load a review session and redirect to the merge UI."""
    session = agent_schema.get_session(session_id)
    if session is None:
        flash('Session not found.', 'error')
        return redirect(url_for('agent.sessions'))

    if session['status'] not in ('completed', 'review'):
        flash(f'Session is not ready for review (status: {session["status"]}).', 'warning')
        return redirect(url_for('agent.session_detail', session_id=session_id))

    try:
        # Create or reuse the review session
        merge_sid = session.get('merge_session_id')
        if not merge_sid:
            merge_sid = _create_review_session(session_id)

        # Switch file_merger to the review session
        return redirect(url_for('switch_session', session_id=merge_sid))
    except ValueError as e:
        flash(str(e), 'error')
        return redirect(url_for('agent.session_detail', session_id=session_id))


# ---------------------------------------------------------------------------
# Merge-back: accepted changes → original repo
# ---------------------------------------------------------------------------

@agent_bp.route('/session/<session_id>/merge', methods=['POST'])
def merge_session(session_id):
    """Execute merge-back: copy accepted agent changes to the original repo."""
    session = agent_schema.get_session(session_id)
    if session is None:
        flash('Session not found.', 'error')
        return redirect(url_for('agent.sessions'))

    if session['status'] != 'review':
        flash('Session must be in review status to merge.', 'warning')
        return redirect(url_for('agent.session_detail', session_id=session_id))

    repo = agent_schema.get_repo(session['repo_id'])
    if repo is None:
        flash('Associated repo not found.', 'error')
        return redirect(url_for('agent.session_detail', session_id=session_id))

    fm = _get_file_merger()
    merge_sid = session.get('merge_session_id')
    if not merge_sid:
        flash('No review session found.', 'error')
        return redirect(url_for('agent.session_detail', session_id=session_id))

    # Load the merge session inventory to see what was resolved
    inv = fm.load_inventory_state(merge_sid)
    if not inv:
        flash('Review session has no inventory data.', 'error')
        return redirect(url_for('agent.session_detail', session_id=session_id))

    repo_path = repo['repo_path']
    ws = session['workspace_path']
    merged_count = 0
    skipped_count = 0
    errors = []

    mode = request.form.get('mode', 'resolved')  # 'resolved' or 'all'

    for rel_path, item in inv.items():
        # Determine which items to merge
        if mode == 'resolved' and not item.resolved:
            skipped_count += 1
            continue
        if mode == 'all' or item.resolved:
            selected = item.selected_version
            if selected is None:
                skipped_count += 1
                continue

            target_path = os.path.join(repo_path, rel_path)

            # Check if source file still exists
            if not os.path.isfile(selected.absolute_path):
                errors.append(f"Source file missing: {selected.absolute_path}")
                continue

            # Check if the selected version is the original (no change)
            if selected.source_name == 'Original':
                # User rejected agent changes — record and skip
                agent_schema.record_review_decision(
                    session_id, rel_path, 'rejected'
                )
                skipped_count += 1
                continue

            # Check if target already has identical content
            if os.path.isfile(target_path):
                target_hash = agent_tools._compute_hash(target_path)
                if target_hash == selected.sha256:
                    skipped_count += 1
                    continue

            # Copy the file
            try:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.copy2(selected.absolute_path, target_path)
                agent_schema.record_review_decision(
                    session_id, rel_path, 'accepted'
                )
                merged_count += 1
            except OSError as e:
                errors.append(f"Failed to copy {rel_path}: {e}")

    # Update session status
    agent_schema.update_session_status(session_id, 'merged')

    msg = f'Merged {merged_count} files to repo.'
    if skipped_count:
        msg += f' Skipped {skipped_count}.'
    if errors:
        msg += f' Errors: {len(errors)}.'
        for err in errors[:3]:
            flash(err, 'error')
    flash(msg, 'success')

    return redirect(url_for('agent.session_detail', session_id=session_id))


@agent_bp.route('/session/<session_id>/reject', methods=['POST'])
def reject_session(session_id):
    """Reject all changes from an agent session."""
    session = agent_schema.get_session(session_id)
    if session is None:
        flash('Session not found.', 'error')
        return redirect(url_for('agent.sessions'))

    # Record rejection for all modified files
    files = agent_schema.get_modified_files(session_id)
    for f in files:
        agent_schema.record_review_decision(session_id, f['relative_path'], 'rejected')

    agent_schema.update_session_status(session_id, 'rejected')
    flash('All changes rejected.', 'info')
    return redirect(url_for('agent.session_detail', session_id=session_id))


# ---------------------------------------------------------------------------
# Conversation routes
# ---------------------------------------------------------------------------

@agent_bp.route('/session/<session_id>/conversation')
def conversation(session_id):
    """View agent conversation history."""
    session = agent_schema.get_session(session_id)
    if session is None:
        flash('Session not found.', 'error')
        return redirect(url_for('agent.sessions'))

    messages = agent_schema.get_conversation(session_id)
    repo = agent_schema.get_repo(session['repo_id'])

    return render_template('agent_conversation.html',
                           session=session,
                           repo=repo,
                           messages=messages)


# ---------------------------------------------------------------------------
# API endpoints for AJAX
# ---------------------------------------------------------------------------

@agent_bp.route('/api/session/<session_id>/files')
def api_session_files(session_id):
    """JSON: list of files in a session with stats."""
    files = agent_schema.get_session_files(session_id)
    return jsonify(files=files)


@agent_bp.route('/api/session/<session_id>/review-summary')
def api_review_summary(session_id):
    """JSON: review progress summary."""
    summary = agent_schema.get_review_summary(session_id)
    return jsonify(summary)


@agent_bp.route('/api/repos')
def api_repos():
    """JSON: list of repos."""
    repos = agent_schema.list_repos()
    return jsonify(repos=repos)


@agent_bp.route('/api/sessions')
def api_sessions():
    """JSON: list of sessions with optional filters."""
    repo_id = request.args.get('repo_id')
    status = request.args.get('status')
    sessions = agent_schema.list_sessions(repo_id=repo_id, status=status)
    return jsonify(sessions=sessions)


# ---------------------------------------------------------------------------
# Multi-Agent Combined Diff View
# ---------------------------------------------------------------------------

# Color palette for agents (up to 6)
AGENT_COLORS = [
    {'name': 'blue',   'bg': 'rgba(59,130,246,0.15)', 'border': '#3b82f6', 'text': '#93c5fd'},
    {'name': 'green',  'bg': 'rgba(34,197,94,0.15)',  'border': '#22c55e', 'text': '#86efac'},
    {'name': 'purple', 'bg': 'rgba(168,85,247,0.15)', 'border': '#a855f7', 'text': '#d8b4fe'},
    {'name': 'orange', 'bg': 'rgba(249,115,22,0.15)', 'border': '#f97316', 'text': '#fdba74'},
    {'name': 'cyan',   'bg': 'rgba(6,182,212,0.15)',  'border': '#06b6d4', 'text': '#67e8f9'},
    {'name': 'pink',   'bg': 'rgba(236,72,153,0.15)', 'border': '#ec4899', 'text': '#f9a8d4'},
]


def _read_file_lines(filepath: str) -> list[str]:
    """Read file lines, stripping trailing newlines."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            return [line.rstrip('\n\r') for line in f.readlines()[:15000]]
    except (OSError, PermissionError):
        return []


def _compute_agent_changes(original_lines: list[str],
                           agent_lines: list[str]) -> list[dict]:
    """Compute changes between original and agent version.
    Returns list of {tag, orig_start, orig_end, agent_start, agent_end}
    for non-equal segments only."""
    matcher = difflib.SequenceMatcher(None, original_lines, agent_lines)
    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != 'equal':
            changes.append({
                'tag': tag,  # replace, insert, delete
                'orig_start': i1,
                'orig_end': i2,
                'agent_start': j1,
                'agent_end': j2,
            })
    return changes


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Check if two line ranges in the original overlap."""
    # Insertions (start==end) at the same point also count as overlap
    if a_start == a_end and b_start == b_end:
        return a_start == b_start
    if a_start == a_end:
        return b_start <= a_start < b_end
    if b_start == b_end:
        return a_start <= b_start < a_end
    return a_start < b_end and b_start < a_end


def generate_multi_agent_diff(original_path: str,
                              agent_versions: list[dict]) -> dict:
    """Generate a combined diff view for multiple agents editing the same file.

    agent_versions: list of {
        'agent_name': str,
        'session_id': str,
        'file_path': str,  # absolute path to agent's version
    }

    Returns {
        'hunks': list of combined hunks,
        'agents': list of {name, color, session_id},
        'has_conflicts': bool,
    }
    """
    original_lines = _read_file_lines(original_path)

    # Assign colors to agents
    agents_info = []
    agent_changes_list = []
    for i, av in enumerate(agent_versions):
        color = AGENT_COLORS[i % len(AGENT_COLORS)]
        agent_lines = _read_file_lines(av['file_path'])
        changes = _compute_agent_changes(original_lines, agent_lines)
        agents_info.append({
            'name': av['agent_name'],
            'session_id': av['session_id'],
            'color': color,
            'lines': agent_lines,
        })
        agent_changes_list.append(changes)

    # Flatten all changes into events on the original line space
    # Each change occupies a range [orig_start, orig_end) in the original
    all_events = []
    for agent_idx, changes in enumerate(agent_changes_list):
        for ch in changes:
            all_events.append({
                'agent_idx': agent_idx,
                'orig_start': ch['orig_start'],
                'orig_end': ch['orig_end'],
                'agent_start': ch['agent_start'],
                'agent_end': ch['agent_end'],
                'tag': ch['tag'],
            })

    # Sort events by their position in the original
    all_events.sort(key=lambda e: (e['orig_start'], e['orig_end']))

    # Find conflict groups: events from different agents that overlap
    # Use a sweep to group overlapping events
    groups = []
    used = set()

    for i, ev in enumerate(all_events):
        if i in used:
            continue
        group = [ev]
        used.add(i)
        # Find all events that overlap with any event in this group
        changed = True
        while changed:
            changed = False
            for j, ev2 in enumerate(all_events):
                if j in used:
                    continue
                # Check overlap with any event in group
                for g_ev in group:
                    if _ranges_overlap(g_ev['orig_start'], g_ev['orig_end'],
                                       ev2['orig_start'], ev2['orig_end']):
                        group.append(ev2)
                        used.add(j)
                        changed = True
                        break
        groups.append(group)

    # Build the combined hunk list
    hunks = []
    hunk_id = 0
    pos = 0  # current position in original lines

    for group in groups:
        group_start = min(e['orig_start'] for e in group)
        group_end = max(e['orig_end'] for e in group)

        # Equal lines before this group
        if pos < group_start:
            hunks.append({
                'id': hunk_id,
                'type': 'equal',
                'lines': original_lines[pos:group_start],
                'start_line': pos + 1,
            })
            hunk_id += 1

        # Determine if this is a conflict (multiple agents) or single-agent change
        agent_indices = set(e['agent_idx'] for e in group)
        is_conflict = len(agent_indices) > 1

        if is_conflict:
            # Build per-agent change data for the conflict
            agent_changes = {}
            for e in group:
                idx = e['agent_idx']
                if idx not in agent_changes:
                    agent_changes[idx] = []
                agent_lines = agents_info[idx]['lines']
                agent_changes[idx].append({
                    'tag': e['tag'],
                    'lines': agent_lines[e['agent_start']:e['agent_end']],
                })

            hunks.append({
                'id': hunk_id,
                'type': 'conflict',
                'original_lines': original_lines[group_start:group_end],
                'start_line': group_start + 1,
                'agents': {
                    idx: {
                        'name': agents_info[idx]['name'],
                        'color': agents_info[idx]['color'],
                        'changes': changes,
                    }
                    for idx, changes in agent_changes.items()
                },
            })
        else:
            # Single agent change — auto-composed
            agent_idx = list(agent_indices)[0]
            agent_info = agents_info[agent_idx]
            # Combine all lines from this agent's changes in this group
            agent_new_lines = []
            for e in sorted(group, key=lambda x: x['agent_start']):
                agent_new_lines.extend(
                    agent_info['lines'][e['agent_start']:e['agent_end']]
                )

            hunks.append({
                'id': hunk_id,
                'type': 'agent_change',
                'agent_idx': agent_idx,
                'agent_name': agent_info['name'],
                'color': agent_info['color'],
                'original_lines': original_lines[group_start:group_end],
                'new_lines': agent_new_lines,
                'start_line': group_start + 1,
            })

        hunk_id += 1
        pos = group_end

    # Trailing equal lines
    if pos < len(original_lines):
        hunks.append({
            'id': hunk_id,
            'type': 'equal',
            'lines': original_lines[pos:],
            'start_line': pos + 1,
        })

    has_conflicts = any(h['type'] == 'conflict' for h in hunks)

    return {
        'hunks': hunks,
        'agents': [{'name': a['name'], 'color': a['color'],
                     'session_id': a['session_id']} for a in agents_info],
        'has_conflicts': has_conflicts,
    }


@agent_bp.route('/combined-diff/<path:filepath>')
def combined_diff(filepath):
    """Multi-agent combined diff view for a file edited by multiple agents.

    Query params:
        sessions: comma-separated session IDs to compare
        repo_id: the repo these sessions belong to
    """
    session_ids = request.args.get('sessions', '').split(',')
    repo_id = request.args.get('repo_id', '')

    if len(session_ids) < 2:
        flash('Need at least 2 session IDs for combined diff.', 'error')
        return redirect(url_for('agent.sessions'))

    repo = agent_schema.get_repo(repo_id) if repo_id else None

    # Build agent versions list
    agent_versions = []
    pairwise_data = []
    original_path = None

    for sid in session_ids:
        sid = sid.strip()
        if not sid:
            continue
        session = agent_schema.get_session(sid)
        if session is None:
            continue

        if repo is None:
            repo = agent_schema.get_repo(session['repo_id'])

        ws = session['workspace_path']
        ws_file = os.path.join(ws, filepath)
        if not os.path.isfile(ws_file):
            continue

        if original_path is None and repo:
            original_path = os.path.join(repo['repo_path'], filepath)

        task_short = session['task_description'][:40]
        agent_versions.append({
            'agent_name': f'{task_short} ({sid[-6:]})',
            'session_id': sid,
            'file_path': ws_file,
        })

    if not original_path or not os.path.isfile(original_path):
        flash(f'Original file not found: {filepath}', 'error')
        return redirect(url_for('agent.sessions'))

    if len(agent_versions) < 2:
        flash('Need at least 2 agent versions of this file.', 'error')
        return redirect(url_for('agent.sessions'))

    # Generate combined diff
    diff_data = generate_multi_agent_diff(original_path, agent_versions)

    # Also generate pairwise diffs for tabs
    fm = _get_file_merger()
    for av in agent_versions:
        try:
            orig_lines = _read_file_lines(original_path)
            agent_lines = _read_file_lines(av['file_path'])
            # Use simple unified diff for pairwise view
            diff_lines = list(difflib.unified_diff(
                orig_lines, agent_lines,
                fromfile='Original', tofile=av['agent_name'],
                lineterm='',
            ))
            pairwise_data.append({
                'agent_name': av['agent_name'],
                'session_id': av['session_id'],
                'diff_lines': diff_lines,
                'added': sum(1 for l in diff_lines if l.startswith('+') and not l.startswith('+++')),
                'removed': sum(1 for l in diff_lines if l.startswith('-') and not l.startswith('---')),
            })
        except Exception:
            pass

    return render_template('agent_combined_diff.html',
                           filepath=filepath,
                           diff_data=diff_data,
                           pairwise_data=pairwise_data,
                           repo=repo,
                           session_ids=session_ids)
