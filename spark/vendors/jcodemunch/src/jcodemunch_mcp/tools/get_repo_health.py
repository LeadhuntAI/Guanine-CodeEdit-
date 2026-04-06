"""get_repo_health — one-call triage snapshot of a repository.

Aggregates results from individual tools to produce a structured health summary:
  - Symbol and file counts
  - Dead-code estimate (% of functions/methods with high dead-code confidence)
  - Average cyclomatic complexity
  - Top 5 hotspots (complexity × churn)
  - Dependency cycle count
  - Unstable module count (instability > 0.7 in the import graph)

Designed to be the *first* tool called in a new session.  One call gives a complete
triage picture with no follow-up needed.  All heavy lifting is delegated to individual
tools — no logic is duplicated here.
"""

from __future__ import annotations

import time
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo
from .get_dead_code_v2 import get_dead_code_v2
from .get_dependency_cycles import get_dependency_cycles
from .get_hotspots import get_hotspots
from .get_dependency_graph import _build_adjacency
from ..parser.imports import resolve_specifier


def _avg_complexity(index) -> float:
    """Mean cyclomatic complexity across all functions/methods with data."""
    values = [
        s.get("cyclomatic") or 0
        for s in index.symbols
        if s.get("kind") in ("function", "method") and (s.get("cyclomatic") or 0) > 0
    ]
    return round(sum(values) / len(values), 2) if values else 0.0


def _count_unstable_modules(index) -> int:
    """Count files with instability > 0.7 (Ce-dominated)."""
    if not index.imports:
        return 0
    source_files = frozenset(index.source_files)
    alias_map = getattr(index, "alias_map", None)
    fwd = _build_adjacency(index.imports, source_files, alias_map)

    # Build reverse (importers per file)
    rev: dict[str, list] = {}
    for src, targets in fwd.items():
        for tgt in targets:
            rev.setdefault(tgt, []).append(src)

    unstable = 0
    for f in index.source_files:
        ca = len(rev.get(f, []))
        ce = len(fwd.get(f, []))
        total = ca + ce
        if total > 0 and (ce / total) > 0.7:
            unstable += 1
    return unstable


def get_repo_health(
    repo: str,
    days: int = 90,
    storage_path: Optional[str] = None,
) -> dict:
    """Return a one-call triage snapshot of the repository.

    Aggregates: symbol counts, dead code %, avg complexity, top hotspots,
    dependency cycle count, and unstable module count.

    Args:
        repo:         Repository identifier (owner/repo or bare name).
        days:         Churn look-back window for hotspot calculation (default 90).
        storage_path: Optional index storage path override.

    Returns:
        ``{repo, summary, top_hotspots, cycle_count, cycles_sample,
           unstable_modules, dead_code_pct, avg_complexity,
           total_symbols, total_files, _meta}``
    """
    t0 = time.perf_counter()

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)
    if index is None:
        return {"error": f"No index found for {repo!r}. Run index_folder first."}

    total_files = len(index.source_files)
    total_symbols = len(index.symbols)
    fn_method_count = sum(
        1 for s in index.symbols if s.get("kind") in ("function", "method")
    )

    # Dead code estimate (min_confidence=0.67 = 2 of 3 signals)
    dead_result = get_dead_code_v2(
        repo=f"{owner}/{name}", min_confidence=0.67, storage_path=storage_path
    )
    dead_count = len(dead_result.get("dead_symbols", []))
    dead_code_pct = (
        round(dead_count / fn_method_count * 100, 1) if fn_method_count > 0 else 0.0
    )

    # Avg complexity
    avg_complexity = _avg_complexity(index)

    # Top hotspots
    hotspot_result = get_hotspots(
        repo=f"{owner}/{name}", top_n=5, days=days, storage_path=storage_path
    )
    top_hotspots = hotspot_result.get("hotspots", [])

    # Dependency cycles
    cycles_result = get_dependency_cycles(
        repo=f"{owner}/{name}", storage_path=storage_path
    )
    cycles = cycles_result.get("cycles", [])
    cycle_count = len(cycles)
    cycles_sample = cycles[:3]  # Show first 3 examples

    # Unstable modules
    unstable_count = _count_unstable_modules(index)

    # Build a human-readable summary line
    health_issues: list[str] = []
    if cycle_count > 0:
        health_issues.append(f"{cycle_count} dependency cycle(s)")
    if dead_code_pct >= 10:
        health_issues.append(f"{dead_code_pct}% likely-dead functions")
    if avg_complexity >= 10:
        health_issues.append(f"avg complexity {avg_complexity} (high)")
    elif avg_complexity >= 5:
        health_issues.append(f"avg complexity {avg_complexity} (medium)")
    if unstable_count > 0:
        health_issues.append(f"{unstable_count} unstable module(s)")

    if not health_issues:
        summary = "Healthy — no major issues detected."
    else:
        summary = "Issues found: " + "; ".join(health_issues) + "."

    elapsed = (time.perf_counter() - t0) * 1000
    return {
        "repo": f"{owner}/{name}",
        "summary": summary,
        "total_files": total_files,
        "total_symbols": total_symbols,
        "fn_method_count": fn_method_count,
        "avg_complexity": avg_complexity,
        "dead_code_pct": dead_code_pct,
        "dead_count": dead_count,
        "cycle_count": cycle_count,
        "cycles_sample": cycles_sample,
        "unstable_modules": unstable_count,
        "top_hotspots": top_hotspots,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "days": days,
        },
    }
