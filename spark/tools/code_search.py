"""Code search tool — wraps jcodemunch IndexStore for Spark agents."""

from __future__ import annotations

import json


def execute(
    action: str = "search",
    query: str = "",
    symbol_id: str = "",
    file_path: str = "",
    kind: str = "",
    file_pattern: str = "",
    limit: int = 20,
    direction: str = "imports",
    depth: int = 1,
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """Search code symbols, get source, or get file outlines.

    Actions:
      search           — BM25 symbol search (requires query)
      get_source       — get source code for a symbol ID (requires symbol_id)
      file_outline     — get all symbols in a file (requires file_path)
      dependency_graph — file-level import/importer graph (requires file_path)
      blast_radius     — find files affected by changing a symbol (requires symbol_id)
      symbol_complexity — get complexity metrics for a symbol (requires symbol_id)
      repo_overview    — high-level repo outline (no params required)
    """
    import os
    from spark.code_index import load_index, get_index_store, get_repo_identifier, _ensure_jcodemunch

    index = load_index(_base_dir)
    if index is None:
        return json.dumps({"error": "Code index not available. Run Spark with code indexing enabled first."})

    try:
        if action == "search":
            if not query:
                return json.dumps({"error": "query is required for search action"})
            results = index.search(
                query=query,
                kind=kind or None,
                file_pattern=file_pattern or None,
                limit=limit,
            )
            summaries = []
            for r in results:
                summaries.append({
                    "id": r.get("id", ""),
                    "name": r.get("name", ""),
                    "kind": r.get("kind", ""),
                    "file": r.get("file", ""),
                    "signature": r.get("signature", ""),
                    "summary": r.get("summary", ""),
                    "line": r.get("line", 0),
                })
            return json.dumps({"results": summaries, "count": len(summaries)})

        elif action == "get_source":
            if not symbol_id:
                return json.dumps({"error": "symbol_id is required for get_source action"})
            store = get_index_store(_base_dir)
            if store is None:
                return json.dumps({"error": "Index store not available"})
            owner, name = get_repo_identifier(_base_dir)
            source = store.get_symbol_content(owner, name, symbol_id, _index=index)
            if source is None:
                return json.dumps({"error": f"Symbol not found: {symbol_id}"})
            return json.dumps({"symbol_id": symbol_id, "source": source})

        elif action == "file_outline":
            if not file_path:
                return json.dumps({"error": "file_path is required for file_outline action"})
            results = index.search(query="", file_pattern=file_path, limit=0)
            symbols = []
            for r in results:
                if r.get("file") == file_path:
                    symbols.append({
                        "id": r.get("id", ""),
                        "name": r.get("name", ""),
                        "kind": r.get("kind", ""),
                        "signature": r.get("signature", ""),
                        "line": r.get("line", 0),
                        "end_line": r.get("end_line", 0),
                    })
            symbols.sort(key=lambda s: s.get("line", 0))
            return json.dumps({"file": file_path, "symbols": symbols, "count": len(symbols)})

        elif action == "dependency_graph":
            if not file_path:
                return json.dumps({"error": "file_path is required for dependency_graph action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_dependency_graph import get_dependency_graph
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_dependency_graph(
                repo=repo_id, file=file_path,
                direction=direction, depth=depth,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "blast_radius":
            if not symbol_id:
                return json.dumps({"error": "symbol_id is required for blast_radius action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_blast_radius import get_blast_radius
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_blast_radius(
                repo=repo_id, symbol=symbol_id,
                depth=depth, storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "symbol_complexity":
            if not symbol_id:
                return json.dumps({"error": "symbol_id is required for symbol_complexity action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_symbol_complexity import get_symbol_complexity
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_symbol_complexity(
                repo=repo_id, symbol_id=symbol_id,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "repo_overview":
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_repo_outline import get_repo_outline
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_repo_outline(repo=repo_id, storage_path=index_path)
            return json.dumps(result)

        else:
            return json.dumps({"error": f"Unknown action: {action}. Use: search, get_source, file_outline, dependency_graph, blast_radius, symbol_complexity, repo_overview"})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
