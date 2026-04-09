# Design: Agent Chat Panel Overhaul

**Date:** 2026-04-08
**Status:** Approved
**Scope:** Right-side chat panel becomes the primary agent interaction surface with session tabs, live OpenCode streaming, direct chat, and sandbox-enforced editing.

---

## Requirements

1. **Session tabs** in the upper area of the right-side chat panel. Each tab shows a truncated session name (first 40 chars of first message) and a colored status indicator dot. Clicking a tab switches the active conversation.
2. **"+" button** creates a new OpenCode session instantly (no form, no modal). Tab appears immediately. First message typed becomes the task instruction.
3. **Live activity streaming** from OpenCode — messages, tool calls, file edits, errors — all rendered in the chat area via SSE.
4. **Direct chat** — user types messages that go to the active OpenCode session instance. Full multi-turn conversation support.
5. **OpenCode is the default backend** with model `openrouter/z-ai/glm-5.1`. No backend/model selector in the panel.
6. **Sandbox enforcement** — OpenCode uses Guanine's MCP tools for all file edits. Reads from the real repo, writes only to the Guanine workspace. Changes go through the review/merge flow before touching the real repo.
7. **Session naming** — tab label is the first 40 characters of the first user message.

## Non-Requirements

- Left sidebar views (explorer, search, dashboard, settings) are not changed.
- The existing review/merge UI is not changed.
- No new database tables or schema changes.
- No model/backend selector in the chat panel (settings page retains this for overrides).

---

## Architecture

### Panel Layout

```
Chat Panel (right side, resizable)
+-------------------------------------------+
| [fix auth bug in logi] [refactor d] [+]   |  <- tab bar (scrollable)
+-------------------------------------------+
| * Running   fix auth bug in login.py      |  <- status bar
+-------------------------------------------+
|                                            |
| You:                                       |
|   Fix the authentication bug in auth.py    |
|                                            |
| Agent:                                     |
|   Let me look at the auth module...        |
|   +- tool: checkout_file ---------------+  |
|   | auth.py -> workspace                |  |
|   +-------------------------------------+  |
|   Found the issue. The token check...      |
|   +- tool: write_file ------------------+  |
|   | auth.py: +5 -2 lines               |  |
|   +-------------------------------------+  |
|   Done. Fixed the token validation.        |
|                                            |
+-------------------------------------------+
| [___Type a message..._____________] [Send] |
+-------------------------------------------+
```

### Tab Bar Behavior

- Horizontal row of tabs, horizontally scrollable if many sessions exist.
- Each tab: colored dot (status) + label (first 40 chars or "New Session" before first message).
- Active tab has a highlighted border/background.
- "+" button is always the rightmost element, pinned.
- Clicking a tab: switches the chat area to that session's conversation, connects SSE to that session.
- Middle-click or close icon on tab: does NOT delete the session, just removes the tab from view. Session continues running.
- Status dot colors: pending (gray), running (blue, pulsing), completed (green), review (yellow), merged (purple), rejected/error (red).

### Session Lifecycle

1. **User clicks "+"**
   - Frontend calls `POST /agent/api/quick-session` with `{repo_id}` (from active project switcher).
   - Backend creates a Guanine `agent_schema` session (status: `running`, backend: `opencode`, model: `openrouter/z-ai/glm-5.1`).
   - Backend ensures OpenCode server is running for the repo (auto-start with `--port 0`).
   - Backend creates an OpenCode session via `POST /session` on the OpenCode server.
   - Backend stores the OpenCode session ID in `backend_session_id`.
   - Returns `{session_id, backend_session_id, status}`.
   - Frontend adds a new tab labeled "New Session", makes it active, focuses the input.
   - Frontend connects SSE to `/agent/api/chat-events/<session_id>`.

2. **User types first message**
   - Frontend calls `POST /agent/api/chat-send/<session_id>` with `{message}`.
   - Backend prepends a **sandbox system prefix** to the first message only (stored in session so it's not re-prepended on subsequent messages).
   - Backend proxies the combined message to OpenCode via `POST /session/{oc_id}/message`.
   - Frontend updates the tab label to first 40 chars of the message.
   - SSE begins streaming: message chunks, tool call starts/results, completion events.

3. **Agent works**
   - OpenCode's AI agent reads project files (CLAUDE.md, AGENTS.md, source code) directly from the real repo — read access is unrestricted.
   - For edits, the agent uses Guanine MCP tools: `checkout_file` to get files into the workspace, `write_file` to edit them.
   - All tool calls appear in the chat as collapsible blocks.
   - File modification events update the session's file tracking in `agent_schema`.

4. **Agent signals done**
   - Agent calls `signal_done` via MCP.
   - Session status transitions to `completed`.
   - Tab dot turns green.
   - A "Review" button appears in the status bar.
   - Clicking "Review" navigates to the existing review/merge UI for that session.

5. **Subsequent messages (multi-turn)**
   - User can send follow-up messages even after the agent's initial response.
   - No sandbox prefix on subsequent messages.
   - OpenCode handles multi-turn conversation natively within its session.

### Sandbox System Prefix

Injected as a prefix to the first user message sent to OpenCode:

```
[SYSTEM] You are working inside Guanine's sandboxed review system.

RULES:
- Use the guanine MCP tools for ALL file modifications
- NEVER use native write or edit tools to modify project files
- Use mcp_guanine_checkout_file to get files into your workspace before editing
- Use mcp_guanine_write_file to write changes (these go to an isolated workspace)
- Use mcp_guanine_read_file or native read to view files (reading is unrestricted)
- Call mcp_guanine_signal_done when your task is complete
- Read CLAUDE.md and AGENTS.md from the repo root for project context and conventions

Your edits will be reviewed by a human before being merged into the actual codebase.
```

This prefix is stored in the session record (`external_context` field) so we know it was already sent and don't re-send it on subsequent messages.

### OpenCode MCP Configuration

OpenCode discovers the Guanine MCP server via its global config at `~/.config/opencode/config.json`:

```json
{
  "mcp": {
    "guanine": {
      "type": "local",
      "command": ["python", "agent_mcp_server.py"],
      "enabled": true
    }
  }
}
```

This config is written automatically by Guanine when the user first starts an OpenCode session (if not already present). The MCP server runs as a subprocess managed by OpenCode — separate from Guanine's own MCP server instance.

The model `openrouter/z-ai/glm-5.1` is available natively in OpenCode via its built-in OpenRouter provider. Requires `OPENROUTER_API_KEY` environment variable (set from the repo's saved API key when starting the OpenCode server).

---

## Data Flow

```
User clicks "+"
  -> POST /agent/api/quick-session {repo_id}
  -> agent_schema.create_session(backend='opencode', model='openrouter/z-ai/glm-5.1')
  -> agent_backends.get_or_start_repo_server(repo_id)
  -> OpenCode POST /session {project_path}
  -> Returns {session_id, backend_session_id}
  -> Tab appears, SSE connects

User sends first message "fix auth bug"
  -> POST /agent/api/chat-send/<sid> {message: "fix auth bug"}
  -> Backend prepends sandbox prefix
  -> OpenCode POST /session/{oc_id}/message {content: "[SYSTEM]...\n\nfix auth bug"}
  -> SSE streams: message.updated, message.part.updated events
  -> Chat renders streamed text + tool blocks

OpenCode agent uses MCP tools
  -> Agent calls mcp_guanine_checkout_file("auth.py")
  -> MCP server: copies auth.py to sessions/<sid>/workspace/
  -> Agent calls mcp_guanine_write_file("auth.py", new_content)
  -> MCP server: writes to workspace, records diff stats
  -> Tool call events appear in SSE -> rendered in chat

Agent calls signal_done
  -> MCP server: reconciles session, status -> completed
  -> SSE event: session status change
  -> Tab dot turns green, Review button appears

User clicks Review
  -> Navigates to /agent/review/<sid>
  -> Existing merge/diff UI with hunk-level accept/reject
```

---

## API Changes

### New Endpoint: `POST /agent/api/quick-session`

Creates a Guanine session + OpenCode session in one call.

**Request:**
```json
{
  "repo_id": "abc123"
}
```

**Response:**
```json
{
  "session_id": "guanine-session-uuid",
  "backend_session_id": "opencode-session-uuid",
  "status": "running",
  "model": "openrouter/z-ai/glm-5.1"
}
```

**Logic:**
1. Look up repo settings for API key.
2. Create `agent_schema` session with `backend='opencode'`, `model='openrouter/z-ai/glm-5.1'`, `task_description='New Session'`.
3. Ensure OpenCode server is running for repo.
4. Create OpenCode session via `POST /session`.
5. Store `backend_session_id`.
6. Ensure OpenCode MCP config includes Guanine server.
7. Return IDs.

### Modified Endpoint: `POST /agent/api/chat-send/<session_id>`

Updated to:
- On first message (detected via empty conversation history), prepend the sandbox system prefix.
- Store prefix in `external_context` to mark it as sent.
- Update `task_description` to first 40 chars of the message.
- Proxy to OpenCode with the agent type set to `build`.

### Modified Endpoint: `GET /agent/api/chat-events/<session_id>`

Updated to:
- For OpenCode sessions: connect to OpenCode's `GET /global/event` SSE endpoint.
- Filter events by session ID.
- Transform OpenCode event format to our normalized format:
  - `message.updated` -> text content chunks
  - `message.part.updated` -> tool call progress
  - `session.updated` -> status changes
  - `session.diff` -> file change notifications

---

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `templates/_chat_panel.html` | **Rewrite** | Replace header with tab bar. Remove backend selector. Add "+" button. Add tab management HTML/CSS. |
| `templates/ide_shell.html` | **Modify** | New JS functions: `chatCreateSession()`, `chatAddTab()`, `chatSwitchTab()`, `chatRemoveTab()`, `chatUpdateTabLabel()`, `chatUpdateTabStatus()`. Rewrite `chatSend()` to handle first-message prefix. Rewrite `chatConnectSSE()` to handle OpenCode event format. Remove `chatSwitchBackend()`. Auto-open chat panel on "+" click. |
| `agent_review.py` | **Modify** | Add `api_quick_session()` endpoint. Update `api_chat_send()` for sandbox prefix injection and task description update. Update `api_chat_events()` for OpenCode SSE event transformation. |
| `agent_backends.py` | **Modify** | Add `ensure_opencode_mcp_config()` function that writes/updates `~/.config/opencode/config.json` with the Guanine MCP server entry if not present. Called during `get_or_start_repo_server()`. |

### What Stays the Same

- `agent_schema.py` — no schema changes needed. Existing fields (`backend`, `backend_session_id`, `external_context`, `task_description`) are sufficient.
- `agent_mcp_server.py` — unchanged. OpenCode calls it directly via its MCP integration.
- `agent_tools.py` — unchanged. MCP server delegates to these.
- Left sidebar views — unchanged.
- Review/merge workflow — unchanged.
- `_dashboard.html` — unchanged (still shows session cards in sidebar).

---

## Edge Cases

1. **No repo selected** — "+" button shows a toast "Select a project first". The project switcher must have an active project.
2. **No API key configured** — `quick-session` endpoint returns an error with a message directing to settings. Toast shown in chat panel.
3. **OpenCode server fails to start** — Error shown in a system message in the chat area (not a toast — keeps context visible).
4. **OpenCode session crashes** — SSE receives an error event. Status dot turns red. System message appears in chat. User can send another message to retry.
5. **Multiple tabs, same session** — Not possible by design. Each session has at most one tab. Re-clicking a session in the dashboard sidebar view activates its existing tab.
6. **Agent ignores sandbox instructions** — This is a best-effort enforcement via the system prefix. The MCP tools are available alongside OpenCode's native tools. A future enhancement could configure OpenCode's agent permissions to disable native write/edit tools, but this is out of scope for now.
7. **Tab overflow** — Tabs scroll horizontally. A small left/right chevron appears when tabs overflow.
