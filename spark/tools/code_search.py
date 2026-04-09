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
    since_sha: str = "",
    include_blast_radius: bool = False,
    class_name: str = "",
    algorithm: str = "pagerank",
    scope: str = "",
    identifier: str = "",
    identifiers: str = "",
    include_call_chain: bool = False,
    include_callers: bool = False,
    output_format: str = "json",
    token_budget: int = 0,
    budget_strategy: str = "most_relevant",
    strategy: str = "combined",
    include_kinds: str = "",
    days: int = 90,
    min_complexity: int = 2,
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """Search code symbols, get source, or get file outlines.

    Actions:
      search             — BM25 symbol search (requires query)
      get_source         — get source code for a symbol ID (requires symbol_id)
      file_outline       — get all symbols in a file (requires file_path)
      dependency_graph   — file-level import/importer graph (requires file_path)
      blast_radius       — find files affected by changing a symbol (requires symbol_id)
      symbol_complexity  — get complexity metrics for a symbol (requires symbol_id)
      repo_overview      — high-level repo outline (no params required)
      coupling_metrics   — afferent/efferent coupling for a file (requires file_path)
      symbol_importance  — rank symbols by PageRank centrality (optional limit, scope)
      changed_symbols    — symbols changed since a git SHA (optional since_sha)
      dependency_cycles  — find circular dependency cycles in the repo
      class_hierarchy    — get inheritance tree for a class (requires class_name)
      find_importers     — find files that import a given file (requires file_path)
      call_hierarchy     — callers and callees for a symbol, N levels deep (requires symbol_id; direction: callers/callees/both)
      impact_preview     — transitive "what breaks?" analysis for a symbol (requires symbol_id)
      related_symbols    — heuristic clustering of related symbols (requires symbol_id; optional limit)
      hotspots           — complexity × churn scoring (optional limit, days, min_complexity)
      repo_health        — one-call triage: dead code, complexity, hotspots, cycles (optional days)
      find_references    — import-level identifier lookup (requires identifier or identifiers; optional include_call_chain)
      context_bundle     — symbol + imports bundle with token budgeting (requires symbol_id; optional token_budget, budget_strategy)
      ranked_context     — BM25 + PageRank query search with token packing (requires query; optional token_budget, strategy)
    """
    import os
    from spark.code_index import load_index, get_index_store, get_repo_identifier, _ensure_jcodemunch

    # LLMs sometimes pass numeric params as strings
    limit = int(limit) if limit is not None else 20
    depth = int(depth) if depth is not None else 1
    days = int(days) if days is not None else 90
    min_complexity = int(min_complexity) if min_complexity is not None else 2
    token_budget = int(token_budget) if token_budget is not None else 0

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

        elif action == "coupling_metrics":
            if not file_path:
                return json.dumps({"error": "file_path is required for coupling_metrics action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_coupling_metrics import get_coupling_metrics
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_coupling_metrics(
                repo=repo_id, module_path=file_path,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "symbol_importance":
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_symbol_importance import get_symbol_importance
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_symbol_importance(
                repo=repo_id, top_n=limit,
                algorithm=algorithm or "pagerank",
                scope=scope or None,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "changed_symbols":
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_changed_symbols import get_changed_symbols
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_changed_symbols(
                repo=repo_id,
                since_sha=since_sha or None,
                include_blast_radius=include_blast_radius,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "dependency_cycles":
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_dependency_cycles import get_dependency_cycles
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_dependency_cycles(
                repo=repo_id, storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "class_hierarchy":
            if not class_name:
                return json.dumps({"error": "class_name is required for class_hierarchy action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_class_hierarchy import get_class_hierarchy
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_class_hierarchy(
                repo=repo_id, class_name=class_name,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "find_importers":
            if not file_path:
                return json.dumps({"error": "file_path is required for find_importers action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.find_importers import find_importers
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = find_importers(
                repo=repo_id, file_path=file_path,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "call_hierarchy":
            if not symbol_id:
                return json.dumps({"error": "symbol_id is required for call_hierarchy action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_call_hierarchy import get_call_hierarchy
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            # call_hierarchy uses callers/callees/both (not imports/importers)
            ch_direction = direction if direction in ("callers", "callees", "both") else "both"
            result = get_call_hierarchy(
                repo=repo_id, symbol_id=symbol_id,
                direction=ch_direction, depth=depth,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "impact_preview":
            if not symbol_id:
                return json.dumps({"error": "symbol_id is required for impact_preview action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_impact_preview import get_impact_preview
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_impact_preview(
                repo=repo_id, symbol_id=symbol_id,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "related_symbols":
            if not symbol_id:
                return json.dumps({"error": "symbol_id is required for related_symbols action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_related_symbols import get_related_symbols
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_related_symbols(
                repo=repo_id, symbol_id=symbol_id,
                max_results=limit, storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "hotspots":
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_hotspots import get_hotspots
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_hotspots(
                repo=repo_id, top_n=limit, days=days,
                min_complexity=min_complexity, storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "repo_health":
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_repo_health import get_repo_health
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_repo_health(
                repo=repo_id, days=days, storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "find_references":
            if not identifier and not identifiers:
                return json.dumps({"error": "identifier or identifiers is required for find_references action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.find_references import find_references as _find_refs
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            ids_list = None
            if identifiers:
                try:
                    ids_list = json.loads(identifiers)
                except (json.JSONDecodeError, TypeError):
                    ids_list = [s.strip() for s in identifiers.split(",") if s.strip()]
            result = _find_refs(
                repo=repo_id, identifier=identifier or None,
                identifiers=ids_list, max_results=limit,
                include_call_chain=include_call_chain,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "context_bundle":
            if not symbol_id:
                return json.dumps({"error": "symbol_id is required for context_bundle action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_context_bundle import get_context_bundle
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            result = get_context_bundle(
                repo=repo_id, symbol_id=symbol_id,
                include_callers=include_callers,
                output_format=output_format,
                token_budget=token_budget or None,
                budget_strategy=budget_strategy,
                storage_path=index_path,
            )
            return json.dumps(result)

        elif action == "ranked_context":
            if not query:
                return json.dumps({"error": "query is required for ranked_context action"})
            if not _ensure_jcodemunch():
                return json.dumps({"error": "jcodemunch not available"})
            from jcodemunch_mcp.tools.get_ranked_context import get_ranked_context
            owner, name = get_repo_identifier(_base_dir)
            repo_id = f"{owner}/{name}"
            index_path = os.path.join(_base_dir, ".code-index")
            kinds_list = None
            if include_kinds:
                try:
                    kinds_list = json.loads(include_kinds)
                except (json.JSONDecodeError, TypeError):
                    kinds_list = [s.strip() for s in include_kinds.split(",") if s.strip()]
            result = get_ranked_context(
                repo=repo_id, query=query,
                token_budget=token_budget or 4000,
                strategy=strategy,
                include_kinds=kinds_list,
                scope=scope or None,
                storage_path=index_path,
            )
            return json.dumps(result)

        else:
            return json.dumps({"error": f"Unknown action: {action}. Use: search, get_source, file_outline, dependency_graph, blast_radius, symbol_complexity, repo_overview, coupling_metrics, symbol_importance, changed_symbols, dependency_cycles, class_hierarchy, find_importers, call_hierarchy, impact_preview, related_symbols, hotspots, repo_health, find_references, context_bundle, ranked_context"})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
