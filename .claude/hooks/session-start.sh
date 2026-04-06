#!/usr/bin/env bash
# SessionStart hook: inject sandbox status and active session info.
# Fires on session start AND after every context compaction.

SCRIPT_DIR="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"

# --- Sandbox status ---
if [ -f "$PROJECT_ROOT/.claude/sandbox-active" ]; then
    echo "=== SANDBOX: ON ==="
else
    echo "=== SANDBOX: OFF ==="
fi

# --- Active session info ---
"$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PROJECT_ROOT'.replace(chr(92), '/'))
os.chdir('$PROJECT_ROOT'.replace(chr(92), '/'))
try:
    import agent_schema
    # Check for running sessions
    sessions = agent_schema.list_sessions(status='running')
    if sessions:
        s = sessions[0]
        print(f'ACTIVE SESSION: {s[\"session_id\"]}')
        print(f'WORKSPACE: {s[\"workspace_path\"]}')
        print(f'TASK: {s[\"task_description\"]}')
    else:
        repos = agent_schema.list_repos()
        if repos:
            print(f'REPO_ID: {repos[0][\"repo_id\"]}')
            print('No active session. Create one with mcp__guanine__create_session.')
        else:
            print('No repos registered. Run: python setup_sandbox.py')
except Exception as e:
    print(f'Guanine status check: {e}')
" 2>/dev/null

# --- Workflow reminder ---
cat <<'EOF'

Sandboxed workflow: create_session -> checkout_file -> Edit in workspace -> signal_done
Toggle: "sandbox off" / "sandbox on" / "edit directly"
Review: http://localhost:5000/agent/sessions
EOF
