#!/usr/bin/env bash
# PreToolUse hook: block Edit/Write to project files outside sessions/
# when sandbox mode is active (.claude/sandbox-active flag file exists).
#
# Exit 0 = allow, Exit 2 = block

# Determine project root (directory containing .claude/)
SCRIPT_DIR="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

# --- Check sandbox flag ---
# If .claude/sandbox-active does NOT exist, sandbox is off — allow everything
if [ ! -f "$PROJECT_ROOT/.claude/sandbox-active" ]; then
    exit 0
fi

# --- Sandbox is ON: check the file path ---
INPUT=$(cat)

# Extract file_path using Python (reliable JSON parsing on Windows)
FILE_PATH=$("$PROJECT_ROOT/.venv/Scripts/python.exe" -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('tool_input', {}).get('file_path', ''))
" <<< "$INPUT" 2>/dev/null)

# If no file_path, allow (safety net for unexpected tool shapes)
if [ -z "$FILE_PATH" ]; then
    exit 0
fi

# Normalize: resolve to absolute, convert to forward slashes, lowercase
NORM_PATH=$("$PROJECT_ROOT/.venv/Scripts/python.exe" -c "
import os, sys
p = sys.argv[1]
p = os.path.realpath(p)
p = p.replace(chr(92), '/').lower()
print(p)
" "$FILE_PATH" 2>/dev/null)

# Normalize project root the same way
NORM_ROOT=$("$PROJECT_ROOT/.venv/Scripts/python.exe" -c "
import os, sys
p = sys.argv[1]
p = os.path.realpath(p)
p = p.replace(chr(92), '/').lower()
print(p)
" "$PROJECT_ROOT" 2>/dev/null)

# If not under project root, allow (e.g. ~/.claude/ paths for plans, memory)
case "$NORM_PATH" in
    ${NORM_ROOT}/*)
        # Under project root — check if it's in sessions/
        case "$NORM_PATH" in
            ${NORM_ROOT}/sessions/*)
                # Workspace file — ALLOW
                exit 0
                ;;
            *)
                # Project file outside sessions — BLOCK
                echo "SANDBOX ACTIVE: Cannot edit project files directly. Use the sandboxed workflow:" >&2
                echo "  1. mcp__guanine__create_session(repo_id, task_description) — or reuse current session" >&2
                echo "  2. mcp__guanine__checkout_file(path) — copy file to workspace" >&2
                echo "  3. Edit/Write files in the workspace (under sessions/)" >&2
                echo "  4. mcp__guanine__signal_done(summary) — when finished" >&2
                echo "" >&2
                echo "To disable sandbox: rm .claude/sandbox-active" >&2
                echo "To edit one file directly: rm .claude/sandbox-active, edit, then touch .claude/sandbox-active" >&2
                exit 2
                ;;
        esac
        ;;
    *)
        # Not under project root — allow
        exit 0
        ;;
esac
