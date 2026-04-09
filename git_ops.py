"""
Git operations for remote project management.

Handles cloning, branching, pulling, pushing, and deploy hooks for
git-based remote projects. All operations use subprocess calls to
the git CLI — no Python git libraries required.

Repo types:
    - local: Existing directory on disk (default, unchanged)
    - git: Cloned from a URL, agents work on branches, push on merge
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Where cloned repos live
_REPOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions', 'repos')


def _run_git(args: list[str], cwd: str, timeout: int = 120) -> dict:
    """Run a git command and return structured result."""
    cmd = ['git'] + args
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return {
            'ok': result.returncode == 0,
            'stdout': result.stdout.strip(),
            'stderr': result.stderr.strip(),
            'returncode': result.returncode,
            'command': ' '.join(cmd),
        }
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'Git command timed out after {timeout}s', 'command': ' '.join(cmd)}
    except FileNotFoundError:
        return {'ok': False, 'error': 'git not found on PATH', 'command': ' '.join(cmd)}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'command': ' '.join(cmd)}


def _run_ssh(host: str, command: str, timeout: int = 60,
             user: Optional[str] = None, port: Optional[int] = None,
             key_path: Optional[str] = None) -> dict:
    """Run a command on a remote host via SSH."""
    ssh_cmd = ['ssh']
    if port:
        ssh_cmd += ['-p', str(port)]
    if key_path:
        ssh_cmd += ['-i', key_path]
    ssh_cmd += ['-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10']

    target = f'{user}@{host}' if user else host
    ssh_cmd += [target, command]

    try:
        result = subprocess.run(
            ssh_cmd, capture_output=True, text=True, timeout=timeout
        )
        return {
            'ok': result.returncode == 0,
            'stdout': result.stdout.strip(),
            'stderr': result.stderr.strip(),
            'returncode': result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'SSH command timed out after {timeout}s'}
    except FileNotFoundError:
        return {'ok': False, 'error': 'ssh not found on PATH'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def clone_repo(git_url: str, repo_id: str, branch: Optional[str] = None) -> dict:
    """Clone a git repository into sessions/repos/<repo_id>/.

    Args:
        git_url: Repository URL (HTTPS or SSH).
        repo_id: Guanine repo ID (used as directory name).
        branch: Branch to checkout (default: repo's default branch).

    Returns:
        Dict with 'ok', 'local_path', and git output.
    """
    os.makedirs(_REPOS_DIR, exist_ok=True)
    local_path = os.path.join(_REPOS_DIR, repo_id)

    if os.path.isdir(local_path):
        # Already cloned — pull instead
        return pull_repo(local_path, branch)

    args = ['clone', '--depth', '1']
    if branch:
        args += ['--branch', branch]
    args += [git_url, local_path]

    result = _run_git(args, cwd=_REPOS_DIR, timeout=300)
    if result['ok']:
        result['local_path'] = local_path
        logger.info("Cloned %s to %s", git_url, local_path)
    else:
        logger.error("Clone failed: %s", result.get('stderr') or result.get('error'))

    return result


def pull_repo(local_path: str, branch: Optional[str] = None) -> dict:
    """Pull latest changes for a cloned repo.

    Fetches and resets to remote HEAD to avoid merge conflicts
    with agent-modified files.
    """
    if not os.path.isdir(local_path):
        return {'ok': False, 'error': f'Directory not found: {local_path}'}

    # Unshallow if needed so we can branch properly
    _run_git(['fetch', '--unshallow'], cwd=local_path, timeout=120)

    # Fetch latest
    fetch = _run_git(['fetch', 'origin'], cwd=local_path, timeout=120)
    if not fetch['ok']:
        return fetch

    # Checkout target branch
    if branch:
        _run_git(['checkout', branch], cwd=local_path)

    # Get current branch
    br = _run_git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=local_path)
    current_branch = br['stdout'] if br['ok'] else 'main'

    # Reset to remote
    reset = _run_git(['reset', '--hard', f'origin/{current_branch}'], cwd=local_path)
    if reset['ok']:
        reset['local_path'] = local_path
        reset['branch'] = current_branch

    return reset


def create_branch(local_path: str, branch_name: str,
                  base_branch: Optional[str] = None) -> dict:
    """Create and checkout a new branch for an agent session.

    Args:
        local_path: Path to the cloned repo.
        branch_name: Name for the new branch.
        base_branch: Base branch to branch from (default: current branch).
    """
    if base_branch:
        _run_git(['checkout', base_branch], cwd=local_path)
        _run_git(['pull', 'origin', base_branch], cwd=local_path, timeout=120)

    result = _run_git(['checkout', '-b', branch_name], cwd=local_path)
    if result['ok']:
        result['branch'] = branch_name
        logger.info("Created branch %s in %s", branch_name, local_path)

    return result


def commit_changes(local_path: str, message: str,
                   author: str = 'Guanine Agent <agent@guanine.local>') -> dict:
    """Stage all changes and commit."""
    # Stage everything
    add = _run_git(['add', '-A'], cwd=local_path)
    if not add['ok']:
        return add

    # Check if there's anything to commit
    status = _run_git(['status', '--porcelain'], cwd=local_path)
    if status['ok'] and not status['stdout'].strip():
        return {'ok': True, 'message': 'Nothing to commit', 'empty': True}

    result = _run_git(['commit', '-m', message, '--author', author], cwd=local_path)
    if result['ok']:
        # Get commit hash
        hash_result = _run_git(['rev-parse', 'HEAD'], cwd=local_path)
        result['commit_hash'] = hash_result['stdout'] if hash_result['ok'] else ''
        logger.info("Committed: %s", result['commit_hash'][:8])

    return result


def push_branch(local_path: str, branch: Optional[str] = None,
                force: bool = False) -> dict:
    """Push a branch to origin."""
    if not branch:
        br = _run_git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=local_path)
        branch = br['stdout'] if br['ok'] else 'main'

    args = ['push', 'origin', branch]
    if force:
        args.insert(1, '--force-with-lease')

    # Unshallow first if needed (can't push from shallow clone)
    _run_git(['fetch', '--unshallow'], cwd=local_path, timeout=120)

    result = _run_git(args, cwd=local_path, timeout=120)
    if result['ok']:
        result['branch'] = branch
        logger.info("Pushed branch %s", branch)

    return result


def get_repo_info(local_path: str) -> dict:
    """Get info about a cloned repo."""
    if not os.path.isdir(os.path.join(local_path, '.git')):
        return {'ok': False, 'error': 'Not a git repository'}

    branch = _run_git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=local_path)
    remote = _run_git(['remote', 'get-url', 'origin'], cwd=local_path)
    log = _run_git(['log', '--oneline', '-5'], cwd=local_path)
    status = _run_git(['status', '--porcelain'], cwd=local_path)

    return {
        'ok': True,
        'branch': branch['stdout'] if branch['ok'] else '',
        'remote_url': remote['stdout'] if remote['ok'] else '',
        'recent_commits': log['stdout'].split('\n') if log['ok'] and log['stdout'] else [],
        'dirty': bool(status['stdout'].strip()) if status['ok'] else False,
        'local_path': local_path,
    }


def list_branches(local_path: str, remote: bool = True) -> dict:
    """List branches in a repo."""
    if remote:
        _run_git(['fetch', 'origin'], cwd=local_path, timeout=60)

    args = ['branch', '-a', '--format=%(refname:short)']
    result = _run_git(args, cwd=local_path)
    if result['ok']:
        branches = [b.strip() for b in result['stdout'].split('\n') if b.strip()]
        result['branches'] = branches

    return result


def generate_branch_name(session_id: str, task: str) -> str:
    """Generate a clean branch name from a session ID and task description."""
    # Clean task to be branch-name-safe
    clean = re.sub(r'[^a-zA-Z0-9\s-]', '', task.lower())
    clean = re.sub(r'\s+', '-', clean.strip())
    clean = clean[:40].rstrip('-')
    if not clean:
        clean = 'agent-task'
    # Use short session ID for uniqueness
    short_id = session_id[:8] if len(session_id) >= 8 else session_id
    return f'guanine/{clean}-{short_id}'


def run_deploy(settings: dict) -> dict:
    """Execute the deploy hook defined in repo settings.

    Settings keys:
        deploy_host: SSH hostname or IP
        deploy_user: SSH username (optional)
        deploy_port: SSH port (optional, default 22)
        deploy_key: Path to SSH private key (optional)
        deploy_command: Command to run on remote host
    """
    host = settings.get('deploy_host', '').strip()
    command = settings.get('deploy_command', '').strip()

    if not host or not command:
        return {'ok': False, 'error': 'Deploy host and command are required'}

    user = settings.get('deploy_user', '').strip() or None
    port = int(settings.get('deploy_port', 0)) or None
    key_path = settings.get('deploy_key', '').strip() or None

    logger.info("Deploying to %s: %s", host, command)
    result = _run_ssh(host, command, user=user, port=port, key_path=key_path)

    if result['ok']:
        logger.info("Deploy successful")
    else:
        logger.error("Deploy failed: %s", result.get('stderr') or result.get('error'))

    return result
