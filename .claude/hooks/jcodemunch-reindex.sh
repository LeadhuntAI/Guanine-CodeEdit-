#!/usr/bin/env bash
# PostToolUse hook: re-index edited file in jcodemunch after Edit/Write.
# Installed by Spark. Runs in ~0.5s per file — invisible to the user.

INPUT=$(cat)

# Extract file_path from the tool input JSON
FILE_PATH=$(python -c "
import sys, json
data = json.load(sys.stdin)
# PostToolUse sends tool_input with file_path
fp = data.get('tool_input', {}).get('file_path', '')
if fp:
    print(fp)
" <<< "$INPUT" 2>/dev/null)

# If no file path extracted, skip
[ -z "$FILE_PATH" ] && exit 0

# Only reindex files that exist and are under the project
[ -f "$FILE_PATH" ] || exit 0

# Run jcodemunch index-file in background (non-blocking)
PYTHONPATH="C:/Projects/360Marketing/Guanine(CodeEdit)/spark/vendors/jcodemunch/src" CODE_INDEX_PATH="C:/Projects/360Marketing/Guanine(CodeEdit)/.code-index" \
    python -m jcodemunch_mcp.server index-file "$FILE_PATH" --no-ai-summaries >/dev/null 2>&1 &

exit 0
