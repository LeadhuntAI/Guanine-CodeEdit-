"""
OpenCode HTTP client for communicating with an OpenCode server instance.

Wraps the OpenCode REST API (default: http://127.0.0.1:4096) using stdlib
urllib only — no new Python dependencies required.

Capabilities:
    - Health check and auto-start of server as subprocess
    - Session creation and management
    - Message sending with agent selection
    - SSE event streaming
    - Agent listing
    - Diff retrieval for review integration
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = 'http://127.0.0.1:4096'
_HEALTH_TIMEOUT = 30  # seconds to wait for server startup
_HEALTH_POLL_INTERVAL = 0.5


class OpenCodeError(Exception):
    """Error communicating with OpenCode server."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class OpenCodeClient:
    """HTTP client for the OpenCode server API.

    Usage:
        client = OpenCodeClient('http://127.0.0.1:4096')
        if not client.health_check():
            client.ensure_server()
        session = client.create_session('/path/to/project', title='Fix auth')
        client.send_message(session['id'], 'Fix the authentication bug')
        for event in client.stream_events(session['id']):
            print(event)
    """

    def __init__(self, base_url: str = _DEFAULT_BASE_URL,
                 password: Optional[str] = None,
                 api_key: Optional[str] = None):
        self.base_url = base_url.rstrip('/')
        self.password = password
        self.api_key = api_key  # OpenRouter API key, set as env var for server
        self._process: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        if self.password:
            headers['Authorization'] = f'Bearer {self.password}'
        return headers

    def _request(self, method: str, path: str,
                 data: Optional[dict] = None,
                 timeout: int = 30) -> dict:
        """Make an HTTP request to the OpenCode server.

        Returns parsed JSON response dict.
        Raises OpenCodeError on failure.
        """
        url = f'{self.base_url}{path}'
        body = json.dumps(data).encode('utf-8') if data else None

        req = urllib.request.Request(
            url, data=body, headers=self._headers(), method=method
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode('utf-8')
                if not raw.strip():
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            body_text = ''
            try:
                body_text = e.read().decode('utf-8', errors='replace')
            except Exception:
                pass
            raise OpenCodeError(
                f'{method} {path} returned {e.code}: {body_text}',
                status_code=e.code
            )
        except urllib.error.URLError as e:
            raise OpenCodeError(f'Connection failed ({url}): {e.reason}')
        except Exception as e:
            raise OpenCodeError(f'Request failed: {e}')

    def _get(self, path: str, timeout: int = 30) -> dict:
        return self._request('GET', path, timeout=timeout)

    def _post(self, path: str, data: Optional[dict] = None,
              timeout: int = 30) -> dict:
        return self._request('POST', path, data=data, timeout=timeout)

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def health_check(self) -> bool:
        """Check if the OpenCode server is reachable and healthy.

        Uses GET /session (returns JSON array) instead of /health,
        because OpenCode v1.4+ serves a web UI SPA that returns HTML
        for unrecognised routes including /health.
        """
        try:
            self._get('/session', timeout=5)
            return True
        except OpenCodeError:
            return False

    def ensure_server(self, port: int = 4096) -> None:
        """Start the OpenCode server if it's not already running.

        Searches for the `opencode` binary on PATH. Starts it with
        ``--port 0`` so the OS auto-assigns a free port (explicit port
        binding fails on some Windows/Bun combinations). The actual
        port is parsed from the server's "listening on" output line and
        ``self.base_url`` is updated accordingly.

        Raises:
            OpenCodeError: If the binary is not found or server fails to start.
        """
        if self.health_check():
            logger.info("OpenCode server already running at %s", self.base_url)
            return

        binary = shutil.which('opencode')
        if not binary:
            raise OpenCodeError(
                "OpenCode binary not found on PATH. "
                "Install it with one of:\n"
                "  npm install -g opencode-ai\n"
                "  go install github.com/anomalyco/opencode@latest\n"
                "  curl -fsSL https://opencode.ai/install | bash\n"
                "Or click 'Install OpenCode' in Guanine settings."
            )

        # Use --port 0 to let the OS pick a free port. Explicit port
        # binding (--port N) fails on some Windows/Bun runtimes even
        # when the port is free.
        logger.info("Starting OpenCode server: %s serve --port 0", binary)
        cmd = [binary, 'serve', '--port', '0', '--print-logs']

        env = os.environ.copy()
        if self.api_key:
            env['OPENROUTER_API_KEY'] = self.api_key
        if self.password:
            env['OPENCODE_PASSWORD'] = self.password

        self._process = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr into stdout
        )

        # Stream subprocess output to the console in a background thread.
        # Also watch for the "listening on http://..." line to learn the
        # actual port the server bound to.
        self._output_lines: list[str] = []
        self._actual_port: int | None = None
        import re as _re
        _port_re = _re.compile(r'listening on https?://[\w.]+:(\d+)')

        def _stream_output(proc):
            oc_logger = logging.getLogger('opencode')
            for raw_line in iter(proc.stdout.readline, b''):
                line = raw_line.decode('utf-8', errors='replace').rstrip()
                # Strip ANSI escape codes for cleaner logs
                clean = _re.sub(r'\x1b\[[0-9;]*m', '', line)
                if clean:
                    oc_logger.info('%s', clean)
                    self._output_lines.append(clean)
                    # Detect actual port from "listening on http://127.0.0.1:XXXX"
                    m = _port_re.search(clean)
                    if m:
                        self._actual_port = int(m.group(1))
                    # Keep buffer bounded
                    if len(self._output_lines) > 500:
                        self._output_lines = self._output_lines[-250:]
            proc.stdout.close()

        t = threading.Thread(target=_stream_output, args=(self._process,),
                             daemon=True)
        t.start()

        # Wait for server to report its port and become healthy
        _port_updated = False
        deadline = time.monotonic() + _HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                recent = '\n'.join(self._output_lines[-20:])
                raise OpenCodeError(
                    f"OpenCode server exited with code {self._process.returncode}:\n{recent}"
                )
            # Once we know the real port, update base_url
            if self._actual_port and not _port_updated:
                self.base_url = f'http://127.0.0.1:{self._actual_port}'
                logger.info("OpenCode auto-assigned port %d", self._actual_port)
                _port_updated = True
            # Only health-check once we know the port
            if _port_updated and self.health_check():
                logger.info("OpenCode server started (PID %d, port %d)",
                            self._process.pid, self._actual_port)
                return
            time.sleep(_HEALTH_POLL_INTERVAL)

        # Timeout — kill the process
        self._process.terminate()
        recent = '\n'.join(self._output_lines[-20:])
        raise OpenCodeError(
            f"OpenCode server did not become healthy within {_HEALTH_TIMEOUT}s.\n"
            f"Last output:\n{recent}"
        )

    def stop_server(self) -> None:
        """Terminate the server process if we started it."""
        if self._process:
            logger.info("Stopping OpenCode server (PID %d)", self._process.pid)
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def create_session(self, project_path: str,
                       title: Optional[str] = None) -> dict:
        """Create a new OpenCode session.

        Args:
            project_path: Absolute path to the project/workspace directory.
            title: Optional session title.

        Returns:
            Dict with session info including 'id'.
        """
        payload = {'path': project_path}
        if title:
            payload['title'] = title
        return self._post('/session', data=payload)

    def get_session(self, session_id: str) -> dict:
        """Get session details."""
        return self._get(f'/session/{session_id}')

    def list_sessions(self) -> list:
        """List all sessions."""
        result = self._get('/session')
        if isinstance(result, list):
            return result
        return result.get('sessions', [])

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def send_message(self, session_id: str, content: str,
                     agent: Optional[str] = None) -> dict:
        """Send a message to an OpenCode session.

        Args:
            session_id: The OpenCode session ID.
            content: Message text.
            agent: Optional agent type (e.g., 'build', 'plan').

        Returns:
            Response dict with message status.
        """
        payload = {
            'parts': [{'type': 'text', 'text': content}],
        }
        if agent:
            payload['agent'] = agent
        return self._post(f'/session/{session_id}/message', data=payload)

    def get_messages(self, session_id: str) -> list:
        """Get message history for a session."""
        result = self._get(f'/session/{session_id}/message')
        if isinstance(result, list):
            return result
        return result.get('messages', [])

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def abort(self, session_id: str) -> dict:
        """Abort a running session."""
        return self._post(f'/session/{session_id}/abort')

    def get_diff(self, session_id: str) -> dict:
        """Get the diff of changes made in a session.

        Used by Guanine's reconciliation step to bridge OpenCode changes
        into the review workflow.
        """
        return self._get(f'/session/{session_id}/diff')

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    def list_agents(self) -> list:
        """List available agent types."""
        result = self._get('/agent')
        if isinstance(result, list):
            return result
        return result.get('agents', [])

    # ------------------------------------------------------------------
    # SSE event streaming
    # ------------------------------------------------------------------

    def stream_events(self, session_id: Optional[str] = None) -> Iterator[dict]:
        """Subscribe to real-time SSE events from the OpenCode server.

        Args:
            session_id: Optional session ID to filter events for.
                        If None, streams all global events.

        Yields:
            Parsed event dicts with 'type' and event-specific data.
        """
        if session_id:
            path = f'/session/{session_id}/event'
        else:
            path = '/global/event'

        url = f'{self.base_url}{path}'
        headers = self._headers()
        headers['Accept'] = 'text/event-stream'

        req = urllib.request.Request(url, headers=headers, method='GET')

        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            logger.error("SSE connection failed: %s", e)
            yield {'type': 'session.error', 'error': str(e)}
            return

        buffer = ''
        event_type = ''
        event_data = ''

        try:
            for raw_line in resp:
                line = raw_line.decode('utf-8', errors='replace').rstrip('\r\n')

                if line.startswith('event:'):
                    event_type = line[6:].strip()
                elif line.startswith('data:'):
                    event_data += line[5:].strip()
                elif line == '':
                    # Empty line = end of event
                    if event_data:
                        try:
                            parsed = json.loads(event_data)
                        except json.JSONDecodeError:
                            parsed = {'raw': event_data}

                        if event_type:
                            parsed['type'] = event_type

                        yield parsed
                    event_type = ''
                    event_data = ''
        except Exception as e:
            logger.warning("SSE stream ended: %s", e)
        finally:
            resp.close()
