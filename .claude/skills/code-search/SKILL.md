---
name: code-search
description: Search code symbols, get source code, and browse file outlines using the jcodemunch index
---

# Code Search Skill

Use the jcodemunch code index to efficiently search and navigate code. The index at `.code-index/` contains AST-parsed symbols for the entire repository.

## Prerequisites

- Python 3.10+ available
- jcodemunch-mcp package installed (`pip install jcodemunch-mcp`)
- Index exists at `.code-index/` (created by Spark)

## Available Commands

### Search symbols by name or keyword

```bash
python -m jcodemunch_mcp.cli search_symbols --repo local/[REPO_NAME] --query "your search query" --storage .code-index/
```

Options:
- `--kind function|class|method|variable` — filter by symbol type
- `--file-pattern "src/**/*.py"` — filter by file glob
- `--limit 20` — max results

### Get source code for a specific symbol

```bash
python -m jcodemunch_mcp.cli get_symbol_source --repo local/[REPO_NAME] --symbol-id "path/file.py::ClassName.method#method" --storage .code-index/
```

### Get file outline (all symbols in a file)

```bash
python -m jcodemunch_mcp.cli get_file_outline --repo local/[REPO_NAME] --file-path "src/main.py" --storage .code-index/
```

### Get blast radius (what would break if a symbol changes)

```bash
python -m jcodemunch_mcp.cli get_blast_radius --repo local/[REPO_NAME] --symbol-id "path/file.py::function#function" --storage .code-index/
```

### Get class hierarchy

```bash
python -m jcodemunch_mcp.cli get_class_hierarchy --repo local/[REPO_NAME] --symbol-id "path/file.py::ClassName#class" --storage .code-index/
```

## When to Use

- **Before reading a file** — use `get_file_outline` to see what's in it, then `get_symbol_source` for specific symbols instead of reading the whole file
- **Finding where something is defined** — use `search_symbols` instead of grep
- **Understanding impact** — use `get_blast_radius` before modifying a function
- **Exploring class structures** — use `get_class_hierarchy`

## Symbol ID Format

Symbol IDs follow the pattern: `file_path::qualified_name#kind`

Examples:
- `src/auth.py::login#function`
- `src/models.py::User#class`
- `src/models.py::User.save#method`
