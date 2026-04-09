#!/usr/bin/env bash
# PreToolUse hook: remind to use jcodemunch MCP tools for code exploration.
# Installed by Spark. Non-blocking — prints a warning, does not abort.

INPUT=$(cat)
TOOL_NAME="$TOOL_NAME"

# Only warn for Read, Grep, Glob on source code files
case "$TOOL_NAME" in
  Read|Grep|Glob) ;;
  *) exit 0 ;;
esac

# Extract the target path from tool input
TARGET=$(python -c "
import sys, json
data = json.load(sys.stdin)
inp = data.get('tool_input', {{}})
# Read uses file_path, Grep/Glob use path or pattern
p = inp.get('file_path', inp.get('path', inp.get('pattern', '')))
print(p)
" <<< "$INPUT" 2>/dev/null)

# Only warn for source code file extensions
case "$TARGET" in
  *.py|*.js|*.ts|*.jsx|*.tsx|*.go|*.rs|*.java|*.rb|*.php|*.cs|*.cpp|*.c|*.h|*.swift|*.kt)
    echo "HINT: jcodemunch MCP tools (search_symbols, get_file_outline, get_symbol_source) are available and understand code structure. Consider using them instead of $TOOL_NAME for code exploration."
    ;;
esac

exit 0
