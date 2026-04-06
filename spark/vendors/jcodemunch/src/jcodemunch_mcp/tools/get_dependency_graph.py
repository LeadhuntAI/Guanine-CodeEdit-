"""Get the file-level dependency graph for a repository file."""

import time
from collections import deque
from typing import Optional

from ..storage import IndexStore
from ..parser.imports import resolve_specifier
from ._utils import resolve_repo
from .package_registry import extract_root_package_from_specifier


def _build_adjacency(
    imports: dict, source_files: frozenset, alias_map: Optional[dict] = None
) -> dict[str, list[str]]:
    """Build forward adjacency {file: [files_it_imports]} from raw import data."""
    adj: dict[str, list[str]] = {}
    for src_file, file_imports in imports.items():
        resolved = []
        for imp in file_imports:
            target = resolve_specifier(imp["specifier"], src_file, source_files, alias_map)
            if target and target != src_file:
                resolved.append(target)
        if resolved:
            adj[src_file] = list(dict.fromkeys(resolved))  # deduplicate, preserve order
    return adj


def _invert(adj: dict[str, list[str]]) -> dict[str, list[str]]:
    """Invert adjacency list: {file: [importers_of_file]}."""
    inv: dict[str, list[str]] = {}
    for src, targets in adj.items():
        for tgt in targets:
            inv.setdefault(tgt, []).append(src)
    return inv


def _bfs(start: str, adj: dict[str, list[str]], depth: int) -> tuple[list[str], list[list[str]]]:
    """BFS from start up to depth hops. Returns (nodes, edges)."""
    visited: dict[str, int] = {start: 0}  # node -> level
    edges: list[list[str]] = []
    queue: deque = deque([(start, 0)])

    while queue:
        node, level = queue.popleft()
        if level >= depth:
            continue
        for neighbor in adj.get(node, []):
            edges.append([node, neighbor])
            if neighbor not in visited:
                visited[neighbor] = level + 1
                queue.append((neighbor, level + 1))

    return list(visited.keys()), edges


def get_dependency_graph(
    repo: str,
    file: str,
    direction: str = "imports",
    depth: int = 1,
    storage_path: Optional[str] = None,
    cross_repo: Optional[bool] = None,
) -> dict:
    """Get the file-level dependency graph for a given file.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        file: File path within the repo (e.g. 'src/server.py').
        direction: 'imports' (files this file depends on), 'importers' (files
            that depend on this file), or 'both'.
        depth: Number of hops to traverse (1–3).
        storage_path: Custom storage path.

    Returns:
        Dict with nodes, edges, per-node neighbor lists, and _meta envelope.
    """
    if direction not in ("imports", "importers", "both"):
        return {"error": f"Invalid direction '{direction}'. Must be 'imports', 'importers', or 'both'."}

    depth = max(1, min(depth, 3))
    start = time.perf_counter()

    # Resolve cross_repo default from config if not explicitly provided
    if cross_repo is None:
        from .. import config as _cfg
        cross_repo = bool(_cfg.get("cross_repo_default", False))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    if index.imports is None:
        return {
            "error": "No import data available. Re-index with jcodemunch-mcp >= 1.3.0 to enable dependency graph."
        }

    if file not in index.source_files:
        return {"error": f"File not found in index: {file}"}

    source_files = frozenset(index.source_files)
    fwd = _build_adjacency(index.imports, source_files, index.alias_map)
    rev = _invert(fwd)

    nodes_out: set[str] = set()
    edges_out: list[list[str]] = []

    if direction in ("imports", "both"):
        ns, es = _bfs(file, fwd, depth)
        nodes_out.update(ns)
        edges_out.extend(es)

    if direction in ("importers", "both"):
        ns, es = _bfs(file, rev, depth)
        nodes_out.update(ns)
        edges_out.extend(es)

    # Deduplicate edges (both directions can overlap at root)
    seen_edges: set[tuple] = set()
    unique_edges = []
    for e in edges_out:
        key = (e[0], e[1])
        if key not in seen_edges:
            seen_edges.add(key)
            unique_edges.append(e)

    # Build per-node neighbor map (only for nodes in our subgraph)
    node_list = sorted(nodes_out)
    neighbors: dict[str, dict] = {}
    for n in node_list:
        entry: dict = {}
        imports_list = [t for t in fwd.get(n, []) if t in nodes_out]
        imported_by_list = [t for t in rev.get(n, []) if t in nodes_out]
        if imports_list:
            entry["imports"] = imports_list
        if imported_by_list:
            entry["imported_by"] = imported_by_list
        neighbors[n] = entry

    # Cross-repo edges: find other repos that publish packages imported by this file
    cross_repo_edges: list[dict] = []
    if cross_repo and index.imports:
        try:
            from .list_repos import list_repos
            all_repos_data = list_repos(storage_path=storage_path).get("repos", [])
            repo_id = f"{owner}/{name}"
            file_imports_for_file = index.imports.get(file, [])
            for imp in file_imports_for_file:
                specifier = imp.get("specifier", "")
                lang = index.file_languages.get(file, "")
                root_pkg = extract_root_package_from_specifier(specifier, lang)
                if not root_pkg:
                    continue
                for repo_entry in all_repos_data:
                    other_repo_id = repo_entry.get("repo", "")
                    if not other_repo_id or other_repo_id == repo_id or "/" not in other_repo_id:
                        continue
                    other_owner, other_name = other_repo_id.split("/", 1)
                    other_index = store.load_index(other_owner, other_name)
                    if not other_index:
                        continue
                    other_pkg_names = getattr(other_index, "package_names", []) or []
                    if root_pkg in other_pkg_names:
                        cross_repo_edges.append({
                            "from": file,
                            "to": specifier,
                            "from_repo": repo_id,
                            "to_repo": other_repo_id,
                            "package_name": root_pkg,
                            "cross_repo": True,
                        })
                        break
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).debug("cross_repo dependency graph failed", exc_info=True)

    elapsed = (time.perf_counter() - start) * 1000
    result = {
        "repo": f"{owner}/{name}",
        "file": file,
        "direction": direction,
        "depth": depth,
        "node_count": len(node_list),
        "edge_count": len(unique_edges),
        "nodes": node_list,
        "edges": unique_edges,
        "neighbors": neighbors,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }
    if cross_repo and cross_repo_edges:
        result["cross_repo_edges"] = cross_repo_edges
    return result
