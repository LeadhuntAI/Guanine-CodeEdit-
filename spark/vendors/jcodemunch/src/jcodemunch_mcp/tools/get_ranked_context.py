"""Standalone token-budgeted context assembler: best-K-tokens for a query."""

import time
from fnmatch import fnmatch
from typing import Optional

from ..storage import IndexStore, record_savings, estimate_savings, cost_avoided as _cost_avoided
from ._utils import resolve_repo
from .get_context_bundle import _count_tokens
from .search_symbols import (
    _tokenize,
    _compute_bm25,
    _bm25_score,
    BYTES_PER_TOKEN,
)

# Weight for PageRank when strategy="combined"
_PR_WEIGHT = 100.0


def get_ranked_context(
    repo: str,
    query: str,
    token_budget: int = 4000,
    strategy: str = "combined",
    include_kinds: Optional[list] = None,
    scope: Optional[str] = None,
    storage_path: Optional[str] = None,
) -> dict:
    """Assemble the best-fit context for a query within a token budget.

    Ranks all symbols by relevance (BM25) and/or centrality (PageRank),
    loads source for the top candidates, and packs greedily until
    ``token_budget`` is exhausted.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        query: Natural language or identifier describing the task.
        token_budget: Hard cap on returned tokens (default 4000).
        strategy: Ranking strategy.
            'combined' (default) = BM25 + PageRank weighted sum.
            'bm25' = pure BM25 text relevance.
            'centrality' = PageRank only (filtered to symbols with BM25 > 0).
        include_kinds: Optional list of symbol kinds to restrict results to
            (e.g. ['class', 'function']).
        scope: Optional subdirectory glob to limit search (e.g. 'src/core/*').
        storage_path: Custom storage path.

    Returns:
        Dict with ``context_items`` list and summary fields.
    """
    _MAX_QUERY_LEN = 500
    if len(query) > _MAX_QUERY_LEN:
        return {"error": f"Query too long ({len(query)} chars, max {_MAX_QUERY_LEN})"}

    if strategy not in ("combined", "bm25", "centrality"):
        return {"error": f"Invalid strategy '{strategy}'. Must be 'combined', 'bm25', or 'centrality'."}

    if token_budget < 1:
        return {"error": "token_budget must be >= 1"}

    start = time.perf_counter()

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # BM25 corpus — cached on CodeIndex
    query_terms = _tokenize(query) or [query.lower()]
    cache = index._bm25_cache
    if "idf" not in cache:
        from .search_symbols import _compute_centrality  # noqa: PLC0415
        cache["idf"], cache["avgdl"], cache["inverted"] = _compute_bm25(index.symbols)
        cache["centrality"] = _compute_centrality(index.symbols, index.imports, index.alias_map)
    idf = cache["idf"]
    avgdl = cache["avgdl"]
    inverted = cache["inverted"]

    # PageRank — computed when strategy requires it
    pagerank: dict = {}
    if strategy in ("centrality", "combined"):
        if "pagerank" not in cache:
            from .pagerank import compute_pagerank  # noqa: PLC0415
            pr_scores, _ = compute_pagerank(
                index.imports or {}, index.source_files, index.alias_map
            )
            cache["pagerank"] = pr_scores
        pagerank = cache["pagerank"]

    # Normalize PageRank to [0,1] for score combination
    max_pr = max(pagerank.values()) if pagerank else 1.0

    # Candidate narrowing via inverted index
    candidate_indices: set[int] = set()
    for term in query_terms:
        posting = inverted.get(term)
        if posting:
            candidate_indices.update(posting)
    candidates = (
        [index.symbols[i] for i in sorted(candidate_indices)]
        if candidate_indices
        else index.symbols
    )

    # Score and filter candidates
    scored: list[tuple[float, float, float, dict]] = []  # (combined, bm25_norm, pr_norm, sym)
    max_bm25 = 0.0
    raw_scores: list[tuple[float, float, dict]] = []  # (bm25, pr_raw, sym)

    for sym in candidates:
        if include_kinds and sym.get("kind") not in include_kinds:
            continue
        if scope and not fnmatch(sym.get("file", ""), scope):
            continue

        bm25 = _bm25_score(sym, query_terms, idf, avgdl)
        if bm25 <= 0 and strategy != "centrality":
            continue
        pr_raw = pagerank.get(sym.get("file", ""), 0.0)
        if bm25 > max_bm25:
            max_bm25 = bm25
        raw_scores.append((bm25, pr_raw, sym))

    if not raw_scores:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "context_items": [],
            "total_tokens": 0,
            "budget_tokens": token_budget,
            "items_included": 0,
            "items_considered": 0,
            "_meta": {
                "timing_ms": round(elapsed, 1),
                "tokens_saved": 0,
                "total_tokens_saved": 0,
            },
        }

    # Normalize and compute combined score
    norm_bm25_denom = max_bm25 if max_bm25 > 0 else 1.0
    norm_pr_denom = max_pr if max_pr > 0 else 1.0

    for bm25, pr_raw, sym in raw_scores:
        bm25_norm = bm25 / norm_bm25_denom
        pr_norm = pr_raw / norm_pr_denom
        if strategy == "bm25":
            combined = bm25_norm
        elif strategy == "centrality":
            combined = pr_norm
        else:  # combined
            combined = 0.5 * bm25_norm + 0.5 * pr_norm
        scored.append((combined, bm25_norm, pr_norm, sym))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Greedy pack: load source and accumulate until budget exhausted
    context_items: list[dict] = []
    total_tokens = 0
    items_considered = len(scored)

    for combined_score, bm25_norm, pr_norm, sym in scored:
        source = store.get_symbol_content(owner, name, sym["id"], _index=index) or ""
        item_tokens = _count_tokens(source) if source else max(1, sym.get("byte_length", 0) // BYTES_PER_TOKEN)
        if total_tokens + item_tokens > token_budget:
            continue  # skip symbols that don't fit; keep trying smaller ones

        context_items.append({
            "symbol_id": sym["id"],
            "relevance_score": round(bm25_norm, 4),
            "centrality_score": round(pr_norm, 4),
            "combined_score": round(combined_score, 4),
            "tokens": item_tokens,
            "source": source,
        })
        total_tokens += item_tokens

    # Token savings estimate
    raw_bytes = sum(
        index.file_sizes.get(sym.get("file", ""), 0)
        for _, _, _, sym in scored[:items_considered]
    )
    response_bytes = total_tokens * BYTES_PER_TOKEN
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved, tool_name="get_ranked_context")

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "context_items": context_items,
        "total_tokens": total_tokens,
        "budget_tokens": token_budget,
        "items_included": len(context_items),
        "items_considered": items_considered,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **_cost_avoided(tokens_saved, total_saved),
        },
    }
