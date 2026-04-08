"""
Pluggable backend system for agent sessions.

Defines an abstract AgentBackend interface and concrete implementations:
    - BuiltinBackend: Wraps the existing agentic/engine/ (OpenRouter + ReAct/native loop)
    - OpenCodeBackend: Delegates to an OpenCode server via HTTP API

Each repo gets its own OpenCode server on a dynamically assigned port,
enabling true parallel agent work across repos. Multiple sessions within
the same repo share one server (OpenCode handles that natively).

Usage:
    backend = get_backend_for_repo(repo_id, 'opencode')
    ref = backend.start_session(workspace_path, task)
    backend.send_message(ref, "Fix the auth bug")
    for event in backend.subscribe_events(ref):
        handle(event)
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import threading
from abc import ABC, abstractmethod
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Port manager — allocates one OpenCode server per repo
# ---------------------------------------------------------------------------

_BASE_PORT = 4096
_MAX_PORT = 4200  # scan range for available ports

_port_lock = threading.Lock()
# repo_id -> {port, process, client, base_url}
_repo_servers: dict[str, dict] = {}


def _is_port_free(port: int) -> bool:
    """Check if a TCP port is available."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.bind(('127.0.0.1', port))
            return True
    except OSError:
        return False


def _allocate_port() -> int:
    """Find the next free port starting from _BASE_PORT."""
    used = {s['port'] for s in _repo_servers.values()}
    for port in range(_BASE_PORT, _MAX_PORT):
        if port not in used and _is_port_free(port):
            return port
    raise RuntimeError(f"No free port found in range {_BASE_PORT}-{_MAX_PORT}")


def get_repo_server(repo_id: str) -> Optional[dict]:
    """Get the running server info for a repo, if any."""
    with _port_lock:
        return _repo_servers.get(repo_id)


def get_or_start_repo_server(repo_id: str, api_key: Optional[str] = None,
                              password: Optional[str] = None) -> dict:
    """Get a running OpenCode server for a repo, starting one if needed.

    Returns dict with 'port', 'base_url', 'client', 'process'.
    OpenCode is started with ``--port 0`` (OS auto-assigns), so the
    actual port comes from the client after ``ensure_server`` completes.
    """
    with _port_lock:
        existing = _repo_servers.get(repo_id)
        if existing:
            # Check if still alive
            from agentic.engine.opencode_client import OpenCodeClient
            client = OpenCodeClient(existing['base_url'], password=password)
            if client.health_check():
                return existing
            # Dead — clean up and re-allocate
            logger.warning("OpenCode server for repo %s died, restarting", repo_id)
            _cleanup_server(repo_id)

        from agentic.engine.opencode_client import OpenCodeClient
        # Start with a placeholder URL; ensure_server will update base_url
        # once the server reports its actual port.
        client = OpenCodeClient('http://127.0.0.1:0', password=password, api_key=api_key)
        ensure_opencode_mcp_config()
        client.ensure_server(port=0)

        # Read back the actual port the server bound to
        actual_port = client._actual_port or 0
        base_url = client.base_url

        info = {
            'port': actual_port,
            'base_url': base_url,
            'client': client,
            'process': client._process,
            'repo_id': repo_id,
        }
        _repo_servers[repo_id] = info
        logger.info("OpenCode server for repo %s started on port %d", repo_id, actual_port)
        return info


def stop_repo_server(repo_id: str) -> bool:
    """Stop the OpenCode server for a specific repo."""
    with _port_lock:
        return _cleanup_server(repo_id)


def _cleanup_server(repo_id: str) -> bool:
    """Terminate a repo's server process. Must hold _port_lock."""
    info = _repo_servers.pop(repo_id, None)
    if not info:
        return False
    proc = info.get('process')
    if proc and proc.poll() is None:
        logger.info("Stopping OpenCode server for repo %s (PID %d, port %d)",
                     repo_id, proc.pid, info['port'])
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return True


def stop_all_servers():
    """Shut down all OpenCode server processes. Called on app exit."""
    with _port_lock:
        for repo_id in list(_repo_servers.keys()):
            _cleanup_server(repo_id)
    logger.info("All OpenCode servers stopped")


def list_running_servers() -> list[dict]:
    """Return info about all running OpenCode servers."""
    with _port_lock:
        result = []
        for repo_id, info in _repo_servers.items():
            alive = False
            proc = info.get('process')
            if proc and proc.poll() is None:
                alive = True
            result.append({
                'repo_id': repo_id,
                'port': info['port'],
                'base_url': info['base_url'],
                'pid': proc.pid if proc else None,
                'alive': alive,
            })
        return result


# Clean up on process exit
atexit.register(stop_all_servers)


def ensure_opencode_mcp_config():
    """Ensure OpenCode's global config includes the Guanine MCP server.

    Writes to ~/.config/opencode/config.json if the 'guanine' entry
    is missing. OpenCode reads this on startup to discover MCP servers.
    """
    config_dir = os.path.join(os.path.expanduser('~'), '.config', 'opencode')
    config_path = os.path.join(config_dir, 'config.json')

    existing = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    mcp = existing.get('mcp', {})
    if 'guanine' in mcp:
        return  # Already configured

    # Build the command to run agent_mcp_server.py from this project
    import sys
    server_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agent_mcp_server.py')
    mcp['guanine'] = {
        'type': 'local',
        'command': [sys.executable, server_script],
        'enabled': True,
    }
    existing['mcp'] = mcp

    os.makedirs(config_dir, exist_ok=True)
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(existing, f, indent=2)
    logger.info("Wrote Guanine MCP config to %s", config_path)


def get_repo_settings(repo_id: str) -> dict:
    """Load backend settings for a repo from the database.

    Returns a dict with keys like 'openrouter_api_key', 'default_model',
    'default_backend', 'opencode_url', etc. Returns empty dict if repo
    not found or settings not configured.
    """
    try:
        import agent_schema
        repo = agent_schema.get_repo(repo_id)
        if repo:
            return repo.get('settings') or {}
    except Exception:
        logger.exception('Failed to load settings for repo %s', repo_id)
    return {}


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------

_backends: dict[str, AgentBackend] = {}
_lock = threading.Lock()


def register_backend(name: str, backend: AgentBackend) -> None:
    """Register an agent backend by name."""
    with _lock:
        _backends[name] = backend
        logger.info("Registered agent backend: %s", name)


def get_backend(name: str) -> AgentBackend:
    """Retrieve a registered backend by name.

    Raises:
        KeyError: If the backend is not registered.
    """
    with _lock:
        if name not in _backends:
            raise KeyError(f"Unknown agent backend: {name!r}. "
                           f"Available: {list(_backends.keys())}")
        return _backends[name]


def list_backends() -> list[dict]:
    """Return info about all registered backends."""
    with _lock:
        return [
            {'name': name, 'type': type(b).__name__, 'ready': b.is_ready()}
            for name, b in _backends.items()
        ]


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class AgentBackend(ABC):
    """Interface for agent execution backends.

    Each backend manages communication with an underlying agent system.
    Guanine creates sandboxed workspaces and tracks files independently;
    backends handle the LLM orchestration and tool execution.
    """

    @abstractmethod
    def is_ready(self) -> bool:
        """Check if the backend is available and can accept sessions."""
        ...

    @abstractmethod
    def start_session(self, workspace_path: str, task: str,
                      model: Optional[str] = None,
                      agent_type: Optional[str] = None) -> str:
        """Start a new agent session in the given workspace.

        Args:
            workspace_path: Absolute path to the sandboxed workspace directory.
            task: Human-readable task description.
            model: Optional model identifier (e.g., 'claude-sonnet-4-20250514').
            agent_type: Optional agent type (e.g., 'build', 'plan').

        Returns:
            A backend-specific session reference string.
        """
        ...

    @abstractmethod
    def send_message(self, session_ref: str, message: str) -> dict:
        """Send a user message to the agent session.

        Args:
            session_ref: The backend session reference from start_session().
            message: The user's message text.

        Returns:
            Dict with at minimum {'status': 'sent'|'error', ...}.
        """
        ...

    @abstractmethod
    def abort(self, session_ref: str) -> dict:
        """Abort a running agent session.

        Returns:
            Dict with {'status': 'aborted'|'error', ...}.
        """
        ...

    @abstractmethod
    def get_status(self, session_ref: str) -> dict:
        """Get current status of the agent session.

        Returns:
            Dict with {'status': str, 'agent': str, ...}.
        """
        ...

    @abstractmethod
    def subscribe_events(self, session_ref: str) -> Iterator[dict]:
        """Subscribe to real-time events from the agent session.

        Yields dicts with event data. Common event types:
            - message.start: Agent begins a response
            - message.content: Streaming text chunk
            - message.complete: Agent finished responding
            - tool.start: Agent is calling a tool
            - tool.result: Tool call completed
            - session.complete: Session finished
            - session.error: Session errored

        The iterator blocks until the session ends or is aborted.
        """
        ...

    def get_available_agents(self) -> list[dict]:
        """Return list of agent types supported by this backend.

        Returns:
            List of dicts with {'name': str, 'description': str, 'mode': str}.
            Default returns a single generic agent.
        """
        return [{'name': 'default', 'description': 'Default agent', 'mode': 'primary'}]

    def get_messages(self, session_ref: str) -> list[dict]:
        """Retrieve message history for a session.

        Returns:
            List of message dicts with 'role', 'content', etc.
            Default returns empty list (backends may not store history).
        """
        return []


# ---------------------------------------------------------------------------
# Builtin backend — wraps agentic/engine/
# ---------------------------------------------------------------------------

class BuiltinBackend(AgentBackend):
    """Backend using the built-in agentic engine (OpenRouter + ReAct/native loop).

    This wraps the existing workflow system in agent_workflow.py and the
    agentic loop in agentic/engine/loop.py. Sessions run in-process using
    the existing tool registry.
    """

    def __init__(self):
        self._running_sessions: dict[str, dict] = {}

    def is_ready(self) -> bool:
        """Builtin is always ready (no external dependencies required)."""
        return True

    def start_session(self, workspace_path: str, task: str,
                      model: Optional[str] = None,
                      agent_type: Optional[str] = None) -> str:
        """Create a builtin session reference.

        The actual workflow execution is triggered by send_message().
        Returns workspace_path as the session reference (matches existing pattern).
        """
        ref = workspace_path  # Use workspace path as reference for builtin
        self._running_sessions[ref] = {
            'workspace_path': workspace_path,
            'task': task,
            'model': model or 'openai/gpt-4o-mini',
            'agent_type': agent_type or 'default',
            'status': 'pending',
            'thread': None,
        }
        return ref

    def send_message(self, session_ref: str, message: str) -> dict:
        """Send a message by running the agentic loop in a background thread.

        For the builtin backend, the first message triggers the full workflow.
        Subsequent messages are not supported (the builtin engine runs to completion).
        """
        session = self._running_sessions.get(session_ref)
        if not session:
            return {'status': 'error', 'error': f'Unknown session: {session_ref}'}

        if session['status'] == 'running':
            return {'status': 'error', 'error': 'Session already running'}

        session['status'] = 'running'
        # Actual execution is handled by the caller (agent_mcp_server or agent_workflow)
        # since the builtin engine's run_workflow() is synchronous and blocking.
        return {'status': 'sent'}

    def abort(self, session_ref: str) -> dict:
        """Abort is not fully supported for the builtin backend."""
        session = self._running_sessions.get(session_ref)
        if session:
            session['status'] = 'aborted'
            return {'status': 'aborted'}
        return {'status': 'error', 'error': 'Unknown session'}

    def get_status(self, session_ref: str) -> dict:
        session = self._running_sessions.get(session_ref)
        if not session:
            return {'status': 'unknown'}
        return {'status': session['status'], 'agent': session['agent_type']}

    def subscribe_events(self, session_ref: str) -> Iterator[dict]:
        """Builtin backend does not support real-time event streaming.

        Yields a single session.complete event when the session finishes.
        Future: hook into loop.py to emit events during execution.
        """
        session = self._running_sessions.get(session_ref)
        if not session:
            yield {'type': 'session.error', 'error': 'Unknown session'}
            return
        # For now, yield nothing — the builtin engine runs synchronously.
        # Phase 3 will add event hooks to agentic/engine/loop.py.
        return
        yield  # Make this a generator

    def get_available_agents(self) -> list[dict]:
        return [
            {'name': 'default', 'description': 'Default coding agent (OpenRouter)', 'mode': 'primary'},
        ]


# ---------------------------------------------------------------------------
# OpenCode backend — delegates to OpenCode server
# ---------------------------------------------------------------------------

class OpenCodeBackend(AgentBackend):
    """Backend using an OpenCode server instance.

    Each repo gets its own server on a dynamic port via the port manager.
    Multiple sessions within the same repo share the same server.
    """

    def __init__(self, repo_id: str,
                 auto_start: bool = True,
                 password: Optional[str] = None,
                 api_key: Optional[str] = None):
        self.repo_id = repo_id
        self.auto_start = auto_start
        self.password = password
        self.api_key = api_key

    def _get_url(self) -> str:
        """Get the base URL for this repo's server, starting it if needed."""
        info = get_repo_server(self.repo_id)
        if info:
            return info['base_url']
        if self.auto_start:
            info = get_or_start_repo_server(
                self.repo_id, api_key=self.api_key, password=self.password
            )
            return info['base_url']
        raise RuntimeError(
            f"No OpenCode server running for repo {self.repo_id}. "
            "Enable auto-start or start manually from Settings."
        )

    def _client(self) -> 'OpenCodeClient':
        from agentic.engine.opencode_client import OpenCodeClient
        return OpenCodeClient(self._get_url(), password=self.password)

    def is_ready(self) -> bool:
        info = get_repo_server(self.repo_id)
        if not info:
            return False
        try:
            from agentic.engine.opencode_client import OpenCodeClient
            client = OpenCodeClient(info['base_url'], password=self.password)
            return client.health_check()
        except Exception:
            return False

    def start_session(self, workspace_path: str, task: str,
                      model: Optional[str] = None,
                      agent_type: Optional[str] = None) -> str:
        result = self._client().create_session(workspace_path, title=task)
        return result.get('id', '')

    def send_message(self, session_ref: str, message: str) -> dict:
        return self._client().send_message(session_ref, message)

    def abort(self, session_ref: str) -> dict:
        return self._client().abort(session_ref)

    def get_status(self, session_ref: str) -> dict:
        return self._client().get_session(session_ref)

    def subscribe_events(self, session_ref: str) -> Iterator[dict]:
        yield from self._client().stream_events(session_ref)

    def get_available_agents(self) -> list[dict]:
        try:
            return self._client().list_agents()
        except Exception as e:
            logger.warning("Failed to list OpenCode agents: %s", e)
            return [
                {'name': 'build', 'description': 'Full-access coding agent', 'mode': 'primary'},
                {'name': 'plan', 'description': 'Read-only analysis agent', 'mode': 'primary'},
            ]

    def get_messages(self, session_ref: str) -> list[dict]:
        return self._client().get_messages(session_ref)

    def cleanup(self):
        """Stop this repo's OpenCode server."""
        stop_repo_server(self.repo_id)


# ---------------------------------------------------------------------------
# Backend factory from repo settings
# ---------------------------------------------------------------------------

def get_backend_for_repo(repo_id: str, backend_name: Optional[str] = None) -> AgentBackend:
    """Get a configured backend for a specific repo.

    Reads the repo's settings_json to configure the backend with the
    correct API key, URL, password, etc. Falls back to the global
    registry if no repo-specific settings exist.

    Args:
        repo_id: The repository ID to load settings for.
        backend_name: Override backend name. If None, uses the repo's
                      default_backend setting (or 'builtin').

    Returns:
        A configured AgentBackend instance.
    """
    settings = get_repo_settings(repo_id)
    name = backend_name or settings.get('default_backend', 'builtin')
    api_key = settings.get('openrouter_api_key', '') or os.environ.get('OPENROUTER_API_KEY', '')

    if name == 'opencode':
        return OpenCodeBackend(
            repo_id=repo_id,
            auto_start=settings.get('opencode_auto_start', True),
            password=settings.get('opencode_password') or None,
            api_key=api_key or None,
        )

    # For builtin or unknown, return from registry
    # but set the API key in the environment so the engine can use it
    if api_key:
        os.environ['OPENROUTER_API_KEY'] = api_key

    return get_backend(name)


# ---------------------------------------------------------------------------
# Auto-register backends on import
# ---------------------------------------------------------------------------

def _init_backends():
    """Register the default backends."""
    register_backend('builtin', BuiltinBackend())
    # OpenCode registered lazily — only when first used or explicitly configured
    # To pre-register: register_backend('opencode', OpenCodeBackend())


_init_backends()
