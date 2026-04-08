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
import logging
import os
import shutil
import time
from datetime import datetime
from typing import Optional

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, Response,
)

import agent_schema
import agent_tools
import agent_workflow
import git_ops

logger = logging.getLogger(__name__)

# We import from file_merger at function-call time to avoid circular imports
# at module load. See _get_file_merger() helper below.

agent_bp = Blueprint('agent', __name__, template_folder='templates')


@agent_bp.before_request
def _log_request():
    """Log every request hitting the agent blueprint."""
    logger.info('%s %s', request.method, request.path)


@agent_bp.after_request
def _log_response(response):
    """Log response status for non-200 or slow API calls."""
    if response.status_code >= 400:
        logger.warning('%s %s -> %s', request.method, request.path, response.status)
    return response


@agent_bp.errorhandler(Exception)
def _handle_error(exc):
    """Catch unhandled exceptions and return JSON instead of HTML error pages."""
    logger.exception('Unhandled exception on %s %s', request.method, request.path)
    if request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json':
        return jsonify({'error': str(exc)}), 500
    # Re-raise for non-API routes so Flask can render the normal error page
    raise exc


# Default coding models — prepopulated for new repos, editable by user.
DEFAULT_CODING_MODELS = [
    {'id': 'glm-5.1', 'label': 'GLM 5.1 (reasoning)', 'providers': 'Friendli'},
    {'id': 'glm-5', 'label': 'GLM 5 (reasoning)', 'providers': ''},
    {'id': 'Kimi-K2p5', 'label': 'Kimi K2.5 (reasoning)', 'providers': ''},
    {'id': 'claude-opus-4.6', 'label': 'Claude Opus 4.6 (thinking)', 'providers': ''},
]
DEFAULT_MODEL_ID = 'glm-5.1'


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
    return render_template('agent_repos.html', repos=all_repos,
                           default_models=DEFAULT_CODING_MODELS,
                           default_model_id=DEFAULT_MODEL_ID)


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

        # Backend configuration (stored as JSON in settings_json)
        settings = repo.get('settings', {})
        settings['openrouter_api_key'] = request.form.get('openrouter_api_key', '').strip()
        settings['default_model'] = request.form.get('default_model', 'openai/gpt-4o-mini').strip()
        settings['default_backend'] = request.form.get('default_backend', 'builtin')
        settings['opencode_url'] = request.form.get('opencode_url', 'http://127.0.0.1:4096').strip()
        settings['opencode_password'] = request.form.get('opencode_password', '').strip()
        settings['opencode_auto_start'] = request.form.get('opencode_auto_start') == 'on'
        settings['default_agent'] = request.form.get('default_agent', 'build')
        try:
            settings['max_parallel_sessions'] = int(request.form.get('max_parallel_sessions', '4'))
        except ValueError:
            settings['max_parallel_sessions'] = 4

        # Git & deploy settings
        settings['git_branch'] = request.form.get('git_branch', 'main').strip()
        settings['deploy_host'] = request.form.get('deploy_host', '').strip()
        settings['deploy_user'] = request.form.get('deploy_user', '').strip()
        settings['deploy_command'] = request.form.get('deploy_command', '').strip()
        settings['deploy_key'] = request.form.get('deploy_key', '').strip()
        try:
            settings['deploy_port'] = int(request.form.get('deploy_port', '0')) or 0
        except ValueError:
            settings['deploy_port'] = 0

        # Parse model list from form arrays
        model_ids = request.form.getlist('model_id[]')
        model_labels = request.form.getlist('model_label[]')
        model_providers = request.form.getlist('model_providers[]')
        models = []
        for i, mid in enumerate(model_ids):
            mid = mid.strip()
            if not mid:
                continue
            models.append({
                'id': mid,
                'label': model_labels[i].strip() if i < len(model_labels) else '',
                'providers': model_providers[i].strip() if i < len(model_providers) else '',
            })
        settings['models'] = models

        agent_schema.update_repo(
            repo_id,
            repo_name=repo_name,
            allowed_commands=cmds,
            allow_free_commands=allow_free,
            ignore_patterns=patterns,
            settings_json=json.dumps(settings),
        )
        flash('Settings updated.', 'success')
        return redirect(url_for('agent.repo_settings', repo_id=repo_id))

    return render_template('agent_repos.html', repos=[repo], editing=repo,
                           default_models=DEFAULT_CODING_MODELS,
                           default_model_id=DEFAULT_MODEL_ID)


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
    backend = request.form.get('backend', '').strip() or None

    if not repo_id or not task:
        flash('Repo and task description are required.', 'error')
        return redirect(url_for('agent.sessions'))

    # If no model specified, use repo default, falling back to global default
    if not model:
        repo = agent_schema.get_repo(repo_id)
        if repo:
            settings = repo.get('settings', {})
            model = settings.get('default_model') or DEFAULT_MODEL_ID
            if not backend:
                backend = settings.get('default_backend') or None
        else:
            model = DEFAULT_MODEL_ID

    logger.info('Creating session: repo=%s model=%s backend=%s task=%r',
                repo_id[:12], model, backend, task[:80])
    try:
        session = agent_schema.create_session(
            repo_id=repo_id,
            task_description=task,
            agent_model=model,
            external_context=context,
            backend=backend,
        )
        logger.info('Session created: %s', session['session_id'])
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
    sources = [fm.SourceConfig(name='Agent Workspace', path=ws, priority=1)]
    fm.save_config(sources, repo['repo_path'], set())

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

def _execute_merge_to_repo(session_id: str, mode: str = 'resolved') -> dict:
    """Shared logic: copy accepted agent changes to the original repo.

    Returns dict with keys: ok, merged, skipped, errors, message.
    """
    session = agent_schema.get_session(session_id)
    if session is None:
        return {'ok': False, 'error': 'Session not found'}

    if session['status'] != 'review':
        return {'ok': False, 'error': 'Session must be in review status to merge'}

    repo = agent_schema.get_repo(session['repo_id'])
    if repo is None:
        return {'ok': False, 'error': 'Associated repo not found'}

    fm = _get_file_merger()
    merge_sid = session.get('merge_session_id')
    if not merge_sid:
        return {'ok': False, 'error': 'No review session found'}

    inv = fm.load_inventory_state(merge_sid)
    if not inv:
        return {'ok': False, 'error': 'Review session has no inventory data'}

    repo_path = repo['repo_path']
    merged_count = 0
    skipped_count = 0
    errors = []

    for rel_path, item in inv.items():
        if mode == 'resolved' and not item.resolved:
            skipped_count += 1
            continue
        if mode == 'all' or item.resolved:
            selected = item.selected_version
            if selected is None:
                skipped_count += 1
                continue

            target_path = os.path.join(repo_path, rel_path)

            if not os.path.isfile(selected.absolute_path):
                errors.append(f"Source file missing: {selected.absolute_path}")
                continue

            if selected.source_name == 'Original':
                agent_schema.record_review_decision(
                    session_id, rel_path, 'rejected'
                )
                skipped_count += 1
                continue

            if os.path.isfile(target_path):
                target_hash = agent_tools._compute_hash(target_path)
                if target_hash == selected.sha256:
                    skipped_count += 1
                    continue

            try:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.copy2(selected.absolute_path, target_path)
                agent_schema.record_review_decision(
                    session_id, rel_path, 'accepted'
                )
                merged_count += 1
            except OSError as e:
                errors.append(f"Failed to copy {rel_path}: {e}")

    agent_schema.update_session_status(session_id, 'merged')

    msg = f'Merged {merged_count} files to repo.'
    if skipped_count:
        msg += f' Skipped {skipped_count}.'
    if errors:
        msg += f' Errors: {len(errors)}.'

    return {
        'ok': True, 'merged': merged_count, 'skipped': skipped_count,
        'errors': errors, 'message': msg,
    }


@agent_bp.route('/session/<session_id>/merge', methods=['POST'])
def merge_session(session_id):
    """Execute merge-back: copy accepted agent changes to the original repo."""
    mode = request.form.get('mode', 'resolved')
    result = _execute_merge_to_repo(session_id, mode)

    if not result['ok']:
        flash(result.get('error', 'Unknown error'), 'error')
        return redirect(url_for('agent.session_detail', session_id=session_id))

    flash(result['message'], 'success')
    for err in result.get('errors', [])[:3]:
        flash(err, 'error')

    return redirect(url_for('agent.session_detail', session_id=session_id))


@agent_bp.route('/api/merge-to-repo', methods=['POST'])
def api_merge_to_repo():
    """JSON API: merge agent changes to repo without page redirect."""
    data = request.get_json(silent=True) or {}
    agent_session_id = data.get('agent_session_id', '')
    if not agent_session_id:
        return jsonify(ok=False, error='agent_session_id required'), 400

    mode = data.get('mode', 'resolved')
    result = _execute_merge_to_repo(agent_session_id, mode)
    return jsonify(**result)


@agent_bp.route('/api/merge-preview/<session_id>', methods=['GET'])
def api_merge_preview(session_id):
    """Preview what a merge would do before executing it."""
    session = agent_schema.get_session(session_id)
    if session is None:
        return jsonify(ok=False, error='Session not found'), 404

    repo = agent_schema.get_repo(session['repo_id'])
    if repo is None:
        return jsonify(ok=False, error='Associated repo not found'), 404

    files = agent_schema.get_session_files(session_id)
    repo_path = repo['repo_path']
    preview = []

    for f in files:
        rel_path = f['relative_path']
        status = f.get('status', 'modified')
        entry = {'path': rel_path, 'status': status, 'action': 'write'}

        # Check stale
        repo_file = os.path.join(repo_path, rel_path)
        if os.path.isfile(repo_file):
            current_hash = agent_tools._compute_hash(repo_file)
            checkout_hash = f.get('checkout_hash', '')
            if checkout_hash and current_hash != checkout_hash:
                entry['stale'] = True
            # Check if content is identical to workspace
            workspace_path = os.path.join(session['workspace_path'], rel_path)
            if os.path.isfile(workspace_path):
                ws_hash = agent_tools._compute_hash(workspace_path)
                if current_hash == ws_hash:
                    entry['action'] = 'skip_identical'
        else:
            entry['action'] = 'create'

        # Check review decision
        decisions = agent_schema.get_review_decisions(session_id)
        for d in decisions:
            if d['relative_path'] == rel_path:
                entry['decision'] = d['decision']
                break

        preview.append(entry)

    stale_count = sum(1 for p in preview if p.get('stale'))
    write_count = sum(1 for p in preview if p['action'] in ('write', 'create'))
    skip_count = sum(1 for p in preview if p['action'] == 'skip_identical')

    return jsonify(ok=True, files=preview, stale_count=stale_count,
                   write_count=write_count, skip_count=skip_count,
                   session_status=session['status'])


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


@agent_bp.route('/api/sessions-browse')
def api_sessions_browse():
    """JSON: session list with file paths for the browse sidebar and dashboard.

    Query params:
        all=1 — include all sessions (default: only completed/review/merged)
        repo_id — filter to sessions for this repo
    """
    repo_id_filter = request.args.get('repo_id')
    sessions = agent_schema.list_sessions(repo_id=repo_id_filter)
    include_all = request.args.get('all', '0') == '1'
    result = []
    for s in sessions:
        if not include_all and s.get('status') not in ('completed', 'review', 'merged'):
            continue
        files = agent_schema.get_session_files(s['session_id'])
        decisions = {d['relative_path']: d['decision']
                     for d in agent_schema.get_review_decisions(s['session_id'])}
        file_list = []
        for f in files:
            rp = f.get('relative_path', '')
            file_list.append({
                'path': rp,
                'relative_path': rp,
                'status': f.get('status', 'checked_out'),
                'lines_added': f.get('lines_added', 0),
                'lines_removed': f.get('lines_removed', 0),
                'reviewed': rp in decisions,
                'decision': decisions.get(rp),
            })
        result.append({
            'session_id': s['session_id'],
            'task_description': s.get('task_description', ''),
            'task': s.get('task_description', ''),
            'status': s.get('status', ''),
            'created': s.get('created_at', ''),
            'model': s.get('agent_model', ''),
            'backend': s.get('backend', 'builtin'),
            'parent_session_id': s.get('parent_session_id'),
            'files': file_list,
            'file_count': len(file_list),
            'modified_count': sum(1 for f in file_list if f['status'] in ('modified', 'new')),
        })
    return jsonify(sessions=result)


@agent_bp.route('/api/inline-diff-agent/<session_id>/<path:filepath>')
def inline_diff_agent(session_id, filepath):
    """Compute inline diff between original repo file and agent workspace version."""
    session = agent_schema.get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    repo = agent_schema.get_repo(session['repo_id'])
    if not repo:
        return jsonify({'error': 'Repo not found'}), 404

    original_path = os.path.join(repo['repo_path'], filepath)
    workspace_path = os.path.join(session['workspace_path'], filepath)

    if not os.path.isfile(workspace_path):
        return jsonify({'error': 'File not found in workspace'}), 404

    def _read_lines(path):
        if not os.path.isfile(path):
            return []
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                return [l.rstrip('\n\r') for l in f.readlines()[:15000]]
        except (OSError, PermissionError):
            return []

    lines_a = _read_lines(original_path)
    lines_b = _read_lines(workspace_path)

    matcher = difflib.SequenceMatcher(None, lines_a, lines_b)
    hunks = []
    hunk_id = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            hunks.append({'id': hunk_id, 'type': 'equal',
                          'lines_a': lines_a[i1:i2], 'lines_b': lines_b[j1:j2],
                          'start_a': i1 + 1, 'start_b': j1 + 1})
        else:
            hunks.append({'id': hunk_id, 'type': 'conflict', 'tag': tag,
                          'lines_a': lines_a[i1:i2], 'lines_b': lines_b[j1:j2],
                          'start_a': i1 + 1, 'start_b': j1 + 1})
        hunk_id += 1

    ext = os.path.splitext(filepath)[1].lower()
    lang_map = {'.py': 'python', '.js': 'javascript', '.ts': 'typescript',
                '.jsx': 'jsx', '.tsx': 'tsx', '.html': 'html', '.css': 'css',
                '.json': 'json', '.md': 'markdown', '.sql': 'sql',
                '.yaml': 'yaml', '.yml': 'yaml', '.xml': 'xml', '.sh': 'shell'}

    # Get task context for summary panel
    task_description = session.get('task_description', '')
    done_summary = ''
    conversation = agent_schema.get_conversation(session_id)
    for msg in reversed(conversation):
        if msg.get('role') == 'assistant' and msg.get('content', '').startswith('Task completed.'):
            done_summary = msg['content'].replace('Task completed. Summary: ', '')
            break

    # Check for stale file (repo changed since checkout)
    is_stale = False
    files = agent_schema.get_session_files(session_id)
    for f in files:
        if f['relative_path'] == filepath:
            current_repo_hash = ''
            if os.path.isfile(original_path):
                import hashlib
                h = hashlib.sha256()
                with open(original_path, 'rb') as fh:
                    for chunk in iter(lambda: fh.read(65536), b''):
                        h.update(chunk)
                current_repo_hash = h.hexdigest()
            if current_repo_hash and f.get('checkout_hash') and current_repo_hash != f['checkout_hash']:
                is_stale = True
            break

    return jsonify({
        'filepath': filepath,
        'source_a': 'Original',
        'source_b': 'Agent (' + session_id[:12] + ')',
        'session_id': session_id,
        'language': lang_map.get(ext, ''),
        'hunks': hunks,
        'conflict_count': sum(1 for h in hunks if h['type'] == 'conflict'),
        'task_description': task_description,
        'done_summary': done_summary,
        'is_stale': is_stale,
    })


@agent_bp.route('/api/review-decision', methods=['POST'])
def api_review_decision():
    """Record a review decision for a file in an agent session."""
    data = request.get_json()
    session_id = data.get('session_id')
    filepath = data.get('filepath')
    decision = data.get('decision', 'accepted')
    if not session_id or not filepath:
        return jsonify({'error': 'session_id and filepath required'}), 400

    notes = data.get('notes')
    try:
        agent_schema.record_review_decision(session_id, filepath, decision,
                                            reviewer_notes=notes)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # Check if all modified files have been reviewed
    summary = agent_schema.get_review_summary(session_id)
    all_reviewed = summary['pending'] == 0 and summary['total_files'] > 0
    if all_reviewed:
        session = agent_schema.get_session(session_id)
        if session and session['status'] in ('completed', 'review'):
            agent_schema.update_session_status(session_id, 'merged')

    return jsonify({
        'ok': True,
        'all_reviewed': all_reviewed,
        'summary': summary,
    })


@agent_bp.route('/api/revert-file', methods=['POST'])
def api_revert_file():
    """Revert a single merged file back to its original version."""
    data = request.get_json(silent=True) or {}
    session_id = data.get('session_id', '')
    filepath = data.get('filepath', '')
    if not session_id or not filepath:
        return jsonify(ok=False, error='session_id and filepath required'), 400

    session = agent_schema.get_session(session_id)
    if not session:
        return jsonify(ok=False, error='Session not found'), 404

    repo = agent_schema.get_repo(session['repo_id'])
    if not repo:
        return jsonify(ok=False, error='Repo not found'), 404

    # Get the original file from the workspace checkout
    workspace_path = session['workspace_path']
    checkout_file = os.path.join(workspace_path, filepath)
    files = agent_schema.get_session_files(session_id)
    file_info = next((f for f in files if f['relative_path'] == filepath), None)
    if not file_info:
        return jsonify(ok=False, error='File not found in session'), 404

    repo_path = repo['repo_path']
    target_path = os.path.join(repo_path, filepath)

    # Restore from checkout_hash — read original from git
    checkout_hash = file_info.get('checkout_hash', '')
    if checkout_hash:
        try:
            import subprocess
            result = subprocess.run(
                ['git', 'show', checkout_hash + ':' + filepath.replace('\\', '/')],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, 'w', encoding='utf-8') as f:
                    f.write(result.stdout)
                agent_schema.record_review_decision(session_id, filepath, 'reverted')
                return jsonify(ok=True, message=f'Reverted {filepath}')
        except Exception:
            pass

    # Fallback: restore from original file in workspace (checkout copy)
    original_dir = os.path.join(workspace_path, '.originals')
    original_file = os.path.join(original_dir, filepath)
    if os.path.isfile(original_file):
        try:
            shutil.copy2(original_file, target_path)
            agent_schema.record_review_decision(session_id, filepath, 'reverted')
            return jsonify(ok=True, message=f'Reverted {filepath}')
        except OSError as e:
            return jsonify(ok=False, error=str(e))

    return jsonify(ok=False, error='Cannot find original version to restore')


@agent_bp.route('/api/revert-session', methods=['POST'])
def api_revert_session():
    """Revert all merged files in a session back to originals."""
    data = request.get_json(silent=True) or {}
    session_id = data.get('session_id', '')
    if not session_id:
        return jsonify(ok=False, error='session_id required'), 400

    session = agent_schema.get_session(session_id)
    if not session:
        return jsonify(ok=False, error='Session not found'), 404
    if session['status'] != 'merged':
        return jsonify(ok=False, error='Session must be merged to revert'), 400

    decisions = agent_schema.get_review_decisions(session_id)
    accepted = [d for d in decisions if d['decision'] in ('accepted', 'edited')]

    reverted = 0
    errors = []
    for d in accepted:
        resp = api_revert_file_internal(session_id, d['relative_path'], session)
        if resp.get('ok'):
            reverted += 1
        elif resp.get('error'):
            errors.append(resp['error'])

    # Set session back to review status
    agent_schema.update_session_status(session_id, 'review')

    return jsonify(ok=True, reverted=reverted, errors=errors,
                   message=f'Reverted {reverted} files')


def api_revert_file_internal(session_id, filepath, session=None):
    """Internal revert helper (no HTTP context needed)."""
    if not session:
        session = agent_schema.get_session(session_id)
    if not session:
        return {'ok': False, 'error': 'Session not found'}

    repo = agent_schema.get_repo(session['repo_id'])
    if not repo:
        return {'ok': False, 'error': 'Repo not found'}

    files = agent_schema.get_session_files(session_id)
    file_info = next((f for f in files if f['relative_path'] == filepath), None)
    if not file_info:
        return {'ok': False, 'error': f'File {filepath} not found'}

    repo_path = repo['repo_path']
    target_path = os.path.join(repo_path, filepath)
    checkout_hash = file_info.get('checkout_hash', '')

    if checkout_hash:
        try:
            import subprocess
            result = subprocess.run(
                ['git', 'show', checkout_hash + ':' + filepath.replace('\\', '/')],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, 'w', encoding='utf-8') as f:
                    f.write(result.stdout)
                agent_schema.record_review_decision(session_id, filepath, 'reverted')
                return {'ok': True}
        except Exception:
            pass

    workspace_path = session['workspace_path']
    original_file = os.path.join(workspace_path, '.originals', filepath)
    if os.path.isfile(original_file):
        try:
            shutil.copy2(original_file, target_path)
            agent_schema.record_review_decision(session_id, filepath, 'reverted')
            return {'ok': True}
        except OSError as e:
            return {'ok': False, 'error': str(e)}

    return {'ok': False, 'error': f'No original for {filepath}'}


@agent_bp.route('/api/repos')
def api_repos():
    """JSON: list of repos."""
    repos = agent_schema.list_repos()
    return jsonify(repos=repos)


@agent_bp.route('/api/repo-tree/<repo_id>')
def api_repo_tree(repo_id):
    """Return file tree for a registered repo."""
    repo = agent_schema.get_repo(repo_id)
    if not repo:
        return jsonify({'tree': {}, 'root': '', 'error': 'Repo not found'}), 404

    repo_path = repo['repo_path']
    if not os.path.isdir(repo_path):
        return jsonify({'tree': {}, 'root': repo_path, 'error': 'Path not found on disk'})

    ignore = set(repo.get('ignore_patterns', [])) | {
        '.git', '__pycache__', 'node_modules', '.venv', 'venv',
        '.tox', '.mypy_cache', '.pytest_cache', 'dist', 'build',
        '.next', '.nuxt', '.output', 'sessions',
    }

    tree = {}
    max_files = 5000
    count = 0
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted([d for d in dirs if d not in ignore])
        for fname in sorted(files):
            if count >= max_files:
                break
            rel = os.path.relpath(os.path.join(root, fname), repo_path).replace('\\', '/')
            parts = rel.split('/')
            node = tree
            for part in parts[:-1]:
                if part not in node:
                    node[part] = {'_type': 'dir', 'children': {}}
                node = node[part]['children']
            node[parts[-1]] = {'_type': 'file', 'relative_path': rel}
            count += 1
        if count >= max_files:
            break

    return jsonify({
        'tree': tree,
        'root': repo_path,
        'repo_id': repo_id,
        'repo_name': repo['repo_name'],
        'file_count': count,
    })


@agent_bp.route('/api/repo-file/<repo_id>')
def api_repo_file_content(repo_id):
    """Read file content from a registered repo."""
    repo = agent_schema.get_repo(repo_id)
    if not repo:
        return jsonify({'error': 'Repo not found'}), 404

    rel_path = request.args.get('path', '')
    if not rel_path:
        return jsonify({'error': 'path parameter required'}), 400

    full_path = os.path.normpath(os.path.join(repo['repo_path'], rel_path))
    # Security: ensure path is within repo
    if not full_path.startswith(os.path.normpath(repo['repo_path'])):
        return jsonify({'error': 'Path outside repo'}), 403

    if not os.path.isfile(full_path):
        return jsonify({'error': 'File not found'}), 404

    try:
        size = os.path.getsize(full_path)
        if size > 500_000:
            return jsonify({'error': f'File too large ({size} bytes)', 'size': size})
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        ext = os.path.splitext(rel_path)[1].lstrip('.')
        return jsonify({
            'content': content,
            'path': rel_path,
            'size': size,
            'extension': ext,
            'line_count': content.count('\n') + 1,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@agent_bp.route('/api/quick-add-repo', methods=['POST'])
def api_quick_add_repo():
    """Quick-add a repo by local path or git URL.

    Body JSON:
        path: Local directory path OR git URL (https://, git@)
        name: Display name (optional, auto-detected)
        branch: Default branch for git repos (optional, defaults to repo default)
    """
    data = request.get_json() if request.is_json else {}
    path_or_url = data.get('path', '').strip()
    if not path_or_url:
        return jsonify({'error': 'path required'}), 400

    repo_name = data.get('name', '').strip()
    branch = data.get('branch', '').strip() or None

    # Detect if this is a git URL
    is_git = (path_or_url.startswith('https://') or
              path_or_url.startswith('http://') or
              path_or_url.startswith('git@') or
              path_or_url.endswith('.git'))

    if is_git:
        # Auto-detect name from URL
        if not repo_name:
            repo_name = path_or_url.rstrip('/').split('/')[-1].replace('.git', '')

        # Generate a temp repo_id for the clone directory
        import hashlib
        temp_id = hashlib.sha256(path_or_url.encode()).hexdigest()[:16]

        # Clone the repo
        clone_result = git_ops.clone_repo(path_or_url, temp_id, branch=branch)
        if not clone_result['ok']:
            return jsonify({
                'error': 'Clone failed: ' + (clone_result.get('stderr') or clone_result.get('error', '')),
            }), 500

        local_path = clone_result['local_path']

        try:
            repo = agent_schema.register_repo(
                repo_path=local_path,
                repo_name=repo_name,
                allow_free_commands=True,
            )
            # Store git config in settings
            settings = repo.get('settings', {})
            settings['connection_type'] = 'git'
            settings['git_url'] = path_or_url
            settings['git_branch'] = branch or 'main'
            agent_schema.update_repo(
                repo['repo_id'],
                settings_json=json.dumps(settings),
            )
            repo['settings'] = settings
            return jsonify(repo)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    else:
        # Local path
        repo_path = os.path.normpath(path_or_url)
        if not os.path.isdir(repo_path):
            return jsonify({'error': f'Directory not found: {repo_path}'}), 400

        if not repo_name:
            repo_name = os.path.basename(repo_path)

        try:
            repo = agent_schema.register_repo(
                repo_path=repo_path,
                repo_name=repo_name,
                allow_free_commands=True,
            )
            return jsonify(repo)
        except Exception as e:
            return jsonify({'error': str(e)}), 500


@agent_bp.route('/api/sessions')
def api_sessions():
    """JSON: list of sessions with optional filters."""
    repo_id = request.args.get('repo_id')
    status = request.args.get('status')
    sessions = agent_schema.list_sessions(repo_id=repo_id, status=status)
    return jsonify(sessions=sessions)


@agent_bp.route('/api/session-detail/<session_id>')
def api_session_detail(session_id):
    """JSON API: full session detail for IDE tab rendering."""
    session = agent_schema.get_session(session_id)
    if session is None:
        return jsonify({'error': 'Session not found'}), 404
    repo = agent_schema.get_repo(session['repo_id'])
    files = agent_schema.get_session_files(session_id)
    review_summary = agent_schema.get_review_summary(session_id)
    return jsonify(session=session, repo=repo, files=files, review_summary=review_summary)


@agent_bp.route('/api/conversation/<session_id>')
def api_conversation(session_id):
    """JSON API: conversation history for IDE tab rendering.

    Optional query param ?filter=<filepath> returns only messages
    that reference the given file path (case-insensitive substring match).
    """
    session = agent_schema.get_session(session_id)
    if session is None:
        return jsonify({'error': 'Session not found'}), 404
    messages = agent_schema.get_conversation(session_id)

    file_filter = request.args.get('filter', '').strip()
    if file_filter:
        # Match on filename or path fragment
        fname = os.path.basename(file_filter).lower()
        filtered = []
        for msg in messages:
            content = (msg.get('content') or '').lower()
            tool_calls_str = str(msg.get('tool_calls') or '').lower()
            if fname in content or file_filter.lower() in content \
               or fname in tool_calls_str or file_filter.lower() in tool_calls_str:
                filtered.append(msg)
        messages = filtered

    return jsonify(session=session, messages=messages,
                   filtered=bool(file_filter), filter_path=file_filter)


# ---------------------------------------------------------------------------
# Chat Panel API — Cascade-style agent interaction
# ---------------------------------------------------------------------------

@agent_bp.route('/api/chat-events/<session_id>')
def api_chat_events(session_id):
    """SSE endpoint for real-time chat events from an agent session.

    Proxies events from the appropriate backend (builtin/opencode).
    For OpenCode: streams from OpenCode's SSE API.
    For builtin: yields events as the agentic loop runs (future).
    """
    session = agent_schema.get_session(session_id)
    if session is None:
        def error_stream():
            yield 'event: session.error\ndata: {"error": "Session not found"}\n\n'
        return Response(error_stream(), mimetype='text/event-stream')

    backend_name = session.get('backend', 'builtin')

    def generate():
        try:
            from agent_backends import get_backend_for_repo
            backend = get_backend_for_repo(session['repo_id'], backend_name)
            ref = session.get('backend_session_id') or session_id
            for event in backend.subscribe_events(ref):
                event_type = event.pop('type', 'message')
                yield f'event: {event_type}\ndata: {json.dumps(event)}\n\n'
        except Exception as e:
            yield f'event: session.error\ndata: {json.dumps({"error": str(e)})}\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@agent_bp.route('/api/chat-send/<session_id>', methods=['POST'])
def api_chat_send(session_id):
    """Send a user message to the agent session via its backend."""
    session = agent_schema.get_session(session_id)
    if session is None:
        return jsonify({'error': 'Session not found'}), 404

    data = request.get_json(silent=True) or {}
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'Empty message'}), 400

    backend_name = session.get('backend', 'builtin')

    # Record the user message in conversation history
    agent_schema.save_conversation_message(session_id, 'user', message)

    try:
        from agent_backends import get_backend_for_repo
        backend = get_backend_for_repo(session['repo_id'], backend_name)
        ref = session.get('backend_session_id') or session_id
        result = backend.send_message(ref, message)
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500


@agent_bp.route('/api/quick-session', methods=['POST'])
def api_quick_session():
    """Create a Guanine + OpenCode session in one call.

    Request JSON: {"repo_id": "..."}
    If repo_id is omitted, uses the first registered repo.

    Returns: {"session_id", "backend_session_id", "status", "model"}
    """
    import agent_backends

    data = request.get_json(silent=True) or {}
    repo_id = data.get('repo_id', '').strip()

    try:
        # Resolve repo
        if not repo_id:
            repos = agent_schema.list_repos()
            if not repos:
                return jsonify({'error': 'No repos registered. Go to Settings to add one.'}), 400
            repo_id = repos[0]['repo_id']

        repo = agent_schema.get_repo(repo_id)
        if not repo:
            return jsonify({'error': f'Repo not found: {repo_id}'}), 404

        # Get API key from repo settings
        settings = agent_backends.get_repo_settings(repo_id)
        api_key = settings.get('openrouter_api_key', '') or os.environ.get('OPENROUTER_API_KEY', '')
        password = settings.get('opencode_password') or None

        if not api_key:
            return jsonify({'error': 'No API key configured. Set it in repo settings or OPENROUTER_API_KEY env var.'}), 400

        model = 'openrouter/z-ai/glm-5.1'

        # 1. Create Guanine session
        session = agent_schema.create_session(
            repo_id=repo_id,
            task_description='New Session',
            agent_model=model,
            backend='opencode',
        )
        session_id = session['session_id']

        # 2. Ensure OpenCode server is running
        server_info = agent_backends.get_or_start_repo_server(
            repo_id, api_key=api_key, password=password
        )

        # 3. Create OpenCode session
        from agentic.engine.opencode_client import OpenCodeClient
        client = OpenCodeClient(server_info['base_url'], password=password)
        oc_session = client.create_session(repo['repo_path'], title='New Session')
        oc_session_id = oc_session.get('id', '')

        # 4. Store backend session ID
        db = agent_schema.get_agent_db()
        db.execute(
            'UPDATE agent_sessions SET backend_session_id = ?, status = ? WHERE session_id = ?',
            (oc_session_id, 'running', session_id)
        )
        db.commit()

        logger.info("Quick session created: %s -> OpenCode %s", session_id, oc_session_id)

        return jsonify({
            'session_id': session_id,
            'backend_session_id': oc_session_id,
            'status': 'running',
            'model': model,
            'repo_id': repo_id,
        })

    except Exception as e:
        logger.exception('Quick session creation failed')
        return jsonify({'error': str(e)}), 500


@agent_bp.route('/api/save-api-key', methods=['POST'])
def api_save_api_key():
    """Save the OpenRouter API key to the active repo's settings."""
    try:
        data = request.get_json(silent=True) or {}
        api_key = data.get('api_key', '').strip()
        repo_id = data.get('repo_id', '').strip()

        if not api_key:
            return jsonify({'error': 'No API key provided'}), 400

        # If no repo_id given, try to find the first registered repo
        if not repo_id:
            repos = agent_schema.list_repos()
            if repos:
                repo_id = repos[0]['repo_id']
            else:
                return jsonify({'error': 'No repos registered. Register a repo first.'}), 400

        repo = agent_schema.get_repo(repo_id)
        if not repo:
            return jsonify({'error': 'Repo not found'}), 404

        settings = repo.get('settings') or {}
        settings['openrouter_api_key'] = api_key
        agent_schema.update_repo(repo_id, settings_json=json.dumps(settings))

        # Also set in environment for immediate use
        os.environ['OPENROUTER_API_KEY'] = api_key

        return jsonify({'status': 'saved', 'repo_id': repo_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@agent_bp.route('/api/get-api-key')
def api_get_api_key():
    """Check if an API key is configured (returns masked version)."""
    repos = agent_schema.list_repos()
    for repo in repos:
        settings = repo.get('settings', {})
        key = settings.get('openrouter_api_key', '')
        if key:
            masked = key[:8] + '...' + key[-4:] if len(key) > 12 else '***'
            return jsonify({'configured': True, 'masked_key': masked, 'repo_id': repo['repo_id']})

    env_key = os.environ.get('OPENROUTER_API_KEY', '')
    if env_key:
        masked = env_key[:8] + '...' + env_key[-4:] if len(env_key) > 12 else '***'
        return jsonify({'configured': True, 'masked_key': masked, 'source': 'env'})

    return jsonify({'configured': False})


@agent_bp.route('/api/repo-models/<repo_id>')
def api_repo_models(repo_id):
    """Get configured models for a repo."""
    repo = agent_schema.get_repo(repo_id)
    if not repo:
        return jsonify({'models': DEFAULT_CODING_MODELS, 'default_model': DEFAULT_MODEL_ID})
    settings = repo.get('settings', {})
    models = settings.get('models') or DEFAULT_CODING_MODELS
    return jsonify({
        'models': models,
        'default_model': settings.get('default_model') or DEFAULT_MODEL_ID,
        'default_backend': settings.get('default_backend', 'builtin'),
    })


@agent_bp.route('/api/opencode-status')
def api_opencode_status():
    """Check if OpenCode is installed and the server is running."""
    import shutil
    npm = shutil.which('npm')
    node = shutil.which('node')
    binary = shutil.which('opencode')
    installed = binary is not None

    node_version = None
    if node:
        try:
            import subprocess as sp
            out = sp.run([node, '--version'], capture_output=True, text=True, timeout=5)
            node_version = out.stdout.strip() if out.returncode == 0 else None
        except Exception:
            pass

    # Check running servers via port manager
    import agent_backends
    servers = agent_backends.list_running_servers()
    active_count = sum(1 for s in servers if s['alive'])

    return jsonify({
        'installed': installed,
        'binary_path': binary,
        'server_running': active_count > 0,
        'active_servers': active_count,
        'servers': servers,
        'node_installed': node is not None,
        'node_version': node_version,
        'npm_installed': npm is not None,
    })


@agent_bp.route('/api/opencode-install', methods=['POST'])
def api_opencode_install():
    """Attempt to install OpenCode via npm. Auto-installs Node.js via winget if needed."""
    import subprocess as sp

    npm = shutil.which('npm')

    # If npm is missing, try to install Node.js automatically (Windows: winget, else error)
    if not npm:
        if os.name == 'nt':
            winget = shutil.which('winget')
            if not winget:
                return jsonify({
                    'error': 'Node.js is not installed and winget is unavailable. '
                             'Please install Node.js from https://nodejs.org and try again.'
                }), 400
            try:
                result = sp.run(
                    [winget, 'install', 'OpenJS.NodeJS.LTS',
                     '--accept-source-agreements', '--accept-package-agreements'],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode != 0:
                    return jsonify({
                        'error': 'Failed to install Node.js via winget.',
                        'stderr': result.stderr[-500:] if result.stderr else '',
                        'hint': 'Install Node.js manually from https://nodejs.org',
                    }), 500
                # Refresh PATH so npm is found in this process
                _refresh_path_windows()
                npm = shutil.which('npm')
                if not npm:
                    return jsonify({
                        'error': 'Node.js was installed but npm is not on PATH yet. '
                                 'Please restart Guanine (close and reopen the terminal) and try again.',
                        'node_installed': True,
                    }), 400
            except sp.TimeoutExpired:
                return jsonify({'error': 'Node.js install timed out after 5 minutes.'}), 500
            except Exception as e:
                return jsonify({'error': f'Node.js install failed: {e}'}), 500
        else:
            return jsonify({
                'error': 'Node.js/npm is not installed. '
                         'Install it from https://nodejs.org and try again.'
            }), 400

    try:
        result = sp.run(
            [npm, 'install', '-g', 'opencode-ai'],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            binary = shutil.which('opencode')
            return jsonify({
                'status': 'installed',
                'binary_path': binary,
                'output': result.stdout[-500:] if result.stdout else ''
            })
        else:
            return jsonify({
                'error': 'Install failed',
                'stderr': result.stderr[-500:] if result.stderr else '',
                'stdout': result.stdout[-500:] if result.stdout else ''
            }), 500
    except sp.TimeoutExpired:
        return jsonify({'error': 'Install timed out after 120s'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _refresh_path_windows():
    """Re-read PATH from the Windows registry so newly installed binaries are found."""
    if os.name != 'nt':
        return
    try:
        import winreg
        # User PATH
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Environment') as key:
            user_path, _ = winreg.QueryValueEx(key, 'Path')
        # System PATH
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment') as key:
            sys_path, _ = winreg.QueryValueEx(key, 'Path')
        os.environ['PATH'] = sys_path + ';' + user_path
    except Exception:
        pass


@agent_bp.route('/api/opencode-start', methods=['POST'])
def api_opencode_start():
    """Start an OpenCode server for a repo (or first repo if not specified)."""
    import agent_backends

    repo_id = request.args.get('repo_id', '')
    if not repo_id and request.is_json:
        repo_id = request.json.get('repo_id', '')

    try:
        # Find a repo to start for
        if not repo_id:
            repos = agent_schema.list_repos()
            if not repos:
                return jsonify({'error': 'No repos registered. Register a repo first.'}), 400
            repo_id = repos[0]['repo_id']

        settings = agent_backends.get_repo_settings(repo_id)
        api_key = settings.get('openrouter_api_key', '') or os.environ.get('OPENROUTER_API_KEY', '')
        password = settings.get('opencode_password') or None

        info = agent_backends.get_or_start_repo_server(
            repo_id, api_key=api_key or None, password=password
        )

        return jsonify({
            'status': 'running',
            'repo_id': repo_id,
            'port': info['port'],
            'pid': info['process'].pid if info.get('process') else None,
            'url': info['base_url'],
        })
    except Exception as e:
        logger.exception('OpenCode start failed for repo %s', repo_id)
        return jsonify({'error': str(e)}), 500


@agent_bp.route('/api/opencode-stop', methods=['POST'])
def api_opencode_stop():
    """Stop the OpenCode server for a specific repo."""
    import agent_backends

    repo_id = request.args.get('repo_id', '')
    if not repo_id and request.is_json:
        repo_id = request.json.get('repo_id', '')

    if not repo_id:
        return jsonify({'error': 'repo_id required'}), 400

    stopped = agent_backends.stop_repo_server(repo_id)
    return jsonify({'stopped': stopped, 'repo_id': repo_id})


@agent_bp.route('/api/opencode-servers')
def api_opencode_servers():
    """List all running OpenCode servers."""
    import agent_backends
    return jsonify({'servers': agent_backends.list_running_servers()})


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

@agent_bp.route('/api/git/info/<repo_id>')
def api_git_info(repo_id):
    """Get git info for a repo (branch, remote, dirty status)."""
    repo = agent_schema.get_repo(repo_id)
    if not repo:
        return jsonify({'error': 'Repo not found'}), 404

    settings = repo.get('settings', {})
    conn_type = settings.get('connection_type', 'local')

    # Works for both local and git repos (if the local path is a git repo)
    info = git_ops.get_repo_info(repo['repo_path'])
    info['connection_type'] = conn_type
    info['git_url'] = settings.get('git_url', '')
    return jsonify(info)


@agent_bp.route('/api/git/pull/<repo_id>', methods=['POST'])
def api_git_pull(repo_id):
    """Pull latest changes for a git repo."""
    repo = agent_schema.get_repo(repo_id)
    if not repo:
        return jsonify({'error': 'Repo not found'}), 404

    settings = repo.get('settings', {})
    branch = settings.get('git_branch')
    result = git_ops.pull_repo(repo['repo_path'], branch=branch)
    return jsonify(result)


@agent_bp.route('/api/git/push/<session_id>', methods=['POST'])
def api_git_push(session_id):
    """Commit and push agent changes for a session.

    This creates a branch, commits the workspace changes, and pushes
    to the remote. Used after review+merge to get changes upstream.
    """
    session = agent_schema.get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    repo = agent_schema.get_repo(session['repo_id'])
    if not repo:
        return jsonify({'error': 'Repo not found'}), 404

    settings = repo.get('settings', {})
    repo_path = repo['repo_path']
    task = session.get('task_description', 'agent task')

    # Generate branch name
    branch_name = git_ops.generate_branch_name(session_id, task)

    # Create branch from default
    base_branch = settings.get('git_branch', 'main')
    br_result = git_ops.create_branch(repo_path, branch_name, base_branch=base_branch)
    if not br_result['ok']:
        return jsonify({'error': 'Branch creation failed', 'detail': br_result}), 500

    # Commit
    commit_msg = f"[guanine] {task}\n\nSession: {session_id}"
    commit_result = git_ops.commit_changes(repo_path, commit_msg)
    if not commit_result['ok']:
        return jsonify({'error': 'Commit failed', 'detail': commit_result}), 500
    if commit_result.get('empty'):
        return jsonify({'ok': True, 'message': 'No changes to push', 'branch': branch_name})

    # Push
    push_result = git_ops.push_branch(repo_path, branch_name)
    if not push_result['ok']:
        return jsonify({'error': 'Push failed', 'detail': push_result}), 500

    return jsonify({
        'ok': True,
        'branch': branch_name,
        'commit_hash': commit_result.get('commit_hash', ''),
        'message': f'Pushed to {branch_name}',
    })


@agent_bp.route('/api/git/deploy/<repo_id>', methods=['POST'])
def api_git_deploy(repo_id):
    """Run the deploy hook for a repo."""
    repo = agent_schema.get_repo(repo_id)
    if not repo:
        return jsonify({'error': 'Repo not found'}), 404

    settings = repo.get('settings', {})
    if not settings.get('deploy_host') or not settings.get('deploy_command'):
        return jsonify({'error': 'Deploy not configured. Set deploy_host and deploy_command in repo settings.'}), 400

    result = git_ops.run_deploy(settings)
    return jsonify(result)


@agent_bp.route('/api/git/push-and-deploy/<session_id>', methods=['POST'])
def api_git_push_and_deploy(session_id):
    """Commit, push, and deploy in one step."""
    session = agent_schema.get_session(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    # Push first
    push_resp = api_git_push(session_id)
    push_data = push_resp.get_json()
    if not push_data.get('ok') and 'error' in push_data:
        return push_resp

    # Then deploy
    repo = agent_schema.get_repo(session['repo_id'])
    settings = repo.get('settings', {})
    if settings.get('deploy_host') and settings.get('deploy_command'):
        deploy_result = git_ops.run_deploy(settings)
        push_data['deploy'] = deploy_result
    else:
        push_data['deploy'] = {'skipped': True, 'reason': 'No deploy hook configured'}

    return jsonify(push_data)


@agent_bp.route('/api/git/branches/<repo_id>')
def api_git_branches(repo_id):
    """List branches for a repo."""
    repo = agent_schema.get_repo(repo_id)
    if not repo:
        return jsonify({'error': 'Repo not found'}), 404

    result = git_ops.list_branches(repo['repo_path'])
    return jsonify(result)


@agent_bp.route('/api/reconcile/<session_id>', methods=['POST'])
def api_reconcile(session_id):
    """Reconcile file changes for a session, bridging to review system."""
    session = agent_schema.get_session(session_id)
    if session is None:
        return jsonify({'error': 'Session not found'}), 404

    result = agent_tools.reconcile_session(session_id)
    if 'error' in result:
        return jsonify(result), 400
    return jsonify(result)


@agent_bp.route('/api/chat-abort/<session_id>', methods=['POST'])
def api_chat_abort(session_id):
    """Abort a running agent session via its backend."""
    session = agent_schema.get_session(session_id)
    if session is None:
        return jsonify({'error': 'Session not found'}), 404

    backend_name = session.get('backend', 'builtin')

    try:
        from agent_backends import get_backend_for_repo
        backend = get_backend_for_repo(session['repo_id'], backend_name)
        ref = session.get('backend_session_id') or session_id
        result = backend.abort(ref)

        # Update session status
        agent_schema.update_session_status(session_id, 'rejected',
                                           error_message='Aborted by user')
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500


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


@agent_bp.route('/api/combined-diff/<path:filepath>')
def api_combined_diff(filepath):
    """JSON API: multi-agent combined diff for IDE rendering.

    Query params:
        sessions: comma-separated session IDs to compare
        repo_id: the repo these sessions belong to
    """
    session_ids = request.args.get('sessions', '').split(',')
    repo_id = request.args.get('repo_id', '')

    if len(session_ids) < 2:
        return jsonify(error='Need at least 2 session IDs'), 400

    repo = agent_schema.get_repo(repo_id) if repo_id else None

    agent_versions = []
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

        task_short = (session.get('task_description') or sid)[:40]
        agent_versions.append({
            'agent_name': f'{task_short} ({sid[-6:]})',
            'session_id': sid,
            'file_path': ws_file,
        })

    if not original_path or not os.path.isfile(original_path):
        return jsonify(error=f'Original file not found: {filepath}'), 404

    if len(agent_versions) < 2:
        return jsonify(error='Need at least 2 agent versions'), 400

    diff_data = generate_multi_agent_diff(original_path, agent_versions)
    diff_data['filepath'] = filepath

    return jsonify(**diff_data)
