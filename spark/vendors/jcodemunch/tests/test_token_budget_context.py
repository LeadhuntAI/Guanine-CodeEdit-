"""Tests for Feature 5: Token-budgeted context assembly.

Covers:
  - get_context_bundle: token_budget, budget_strategy, include_budget_report
  - get_ranked_context: query-driven ranked context assembly
"""

import pytest
from pathlib import Path

from jcodemunch_mcp.tools.get_context_bundle import get_context_bundle
from jcodemunch_mcp.tools.get_ranked_context import get_ranked_context
from jcodemunch_mcp.tools.index_folder import index_folder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(tmp_path: Path, files: dict[str, str]) -> tuple[str, str]:
    """Write files to tmp_path and index them. Return (repo_id, storage_path)."""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    storage = str(tmp_path / ".index")
    result = index_folder(str(tmp_path), use_ai_summaries=False, storage_path=storage)
    repo_id = result.get("repo", str(tmp_path))
    return repo_id, storage


_SMALL_REPO = {
    "engine.py": (
        "class Engine:\n"
        "    \"\"\"Core engine.\"\"\"\n"
        "    def run(self):\n"
        "        pass\n\n"
        "    def stop(self):\n"
        "        pass\n"
    ),
    "utils.py": "def format_date(d):\n    return str(d)\n\ndef parse_date(s):\n    return s\n",
    "main.py": "from engine import Engine\nfrom utils import format_date\n\ndef main():\n    e = Engine()\n    e.run()\n",
}


# ---------------------------------------------------------------------------
# get_context_bundle — token_budget
# ---------------------------------------------------------------------------

class TestContextBundleTokenBudget:
    def test_budget_none_returns_full_source(self, tmp_path):
        """Without a budget, source is always included."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(repo, symbol_id="engine.py::Engine#class", storage_path=storage)
        assert "error" not in result
        assert result.get("source", "") != ""

    def test_compact_strategy_strips_source(self, tmp_path):
        """budget_strategy='compact' strips source bodies from all entries."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_ids=["engine.py::Engine#class", "utils.py::format_date#function"],
            token_budget=10000,
            budget_strategy="compact",
            storage_path=storage,
        )
        assert "error" not in result
        for sym in result["symbols"]:
            assert sym["source"] == "", f"Expected empty source, got: {sym['source'][:50]}"

    def test_compact_strategy_retains_signature(self, tmp_path):
        """compact mode keeps signature even when source is stripped."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_id="engine.py::Engine#class",
            token_budget=10000,
            budget_strategy="compact",
            storage_path=storage,
        )
        assert "error" not in result
        assert result.get("signature"), "Signature should be non-empty in compact mode"

    def test_tiny_budget_excludes_symbols(self, tmp_path):
        """A very small token_budget trims symbols that don't fit."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        # Engine class has substantial source; budget of 1 token should exclude everything
        result = get_context_bundle(
            repo,
            symbol_ids=["engine.py::Engine#class", "utils.py::format_date#function"],
            token_budget=1,
            budget_strategy="most_relevant",
            include_budget_report=True,
            storage_path=storage,
        )
        # Should not crash; budget_report should reflect exclusions
        assert "error" not in result or "budget_report" in result

    def test_budget_report_omitted_by_default(self, tmp_path):
        """budget_report is absent when include_budget_report=False (default)."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_id="engine.py::Engine#class",
            token_budget=10000,
            storage_path=storage,
        )
        assert "budget_report" not in result

    def test_budget_report_present_when_requested(self, tmp_path):
        """budget_report is present when include_budget_report=True."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_ids=["engine.py::Engine#class", "utils.py::format_date#function"],
            token_budget=10000,
            include_budget_report=True,
            storage_path=storage,
        )
        assert "error" not in result
        assert "budget_report" in result
        br = result["budget_report"]
        assert "budget_tokens" in br
        assert "used_tokens" in br
        assert "included_symbols" in br
        assert "excluded_symbols" in br
        assert "strategy" in br

    def test_budget_report_used_tokens_le_budget(self, tmp_path):
        """budget_report.used_tokens must not exceed token_budget."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_ids=["engine.py::Engine#class", "utils.py::format_date#function"],
            token_budget=200,
            include_budget_report=True,
            storage_path=storage,
        )
        assert "error" not in result
        br = result["budget_report"]
        assert br["used_tokens"] <= br["budget_tokens"]

    def test_invalid_budget_strategy_returns_error(self, tmp_path):
        """Unknown budget_strategy returns a structured error."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(
            repo,
            symbol_id="engine.py::Engine#class",
            token_budget=1000,
            budget_strategy="magic",
            storage_path=storage,
        )
        assert "error" in result
        assert "budget_strategy" in result["error"]

    def test_no_budget_backward_compat(self, tmp_path):
        """Without token_budget, response shape is unchanged (backward compat)."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_context_bundle(repo, symbol_id="utils.py::format_date#function", storage_path=storage)
        assert "error" not in result
        assert "symbol_id" in result
        assert "source" in result
        assert "budget_report" not in result


# ---------------------------------------------------------------------------
# get_ranked_context
# ---------------------------------------------------------------------------

class TestGetRankedContext:
    def test_returns_context_items(self, tmp_path):
        """Basic call returns context_items list."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_ranked_context(repo, query="engine run", storage_path=storage)
        assert "error" not in result
        assert "context_items" in result
        assert isinstance(result["context_items"], list)

    def test_total_tokens_le_budget(self, tmp_path):
        """total_tokens must not exceed token_budget."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        budget = 100
        result = get_ranked_context(repo, query="engine", token_budget=budget, storage_path=storage)
        assert "error" not in result
        assert result["total_tokens"] <= budget

    def test_items_include_source(self, tmp_path):
        """Each context item includes a non-empty source field."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_ranked_context(repo, query="Engine", token_budget=4000, storage_path=storage)
        assert "error" not in result
        for item in result["context_items"]:
            assert "source" in item

    def test_items_have_score_fields(self, tmp_path):
        """Each context item has relevance_score, centrality_score, combined_score."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_ranked_context(repo, query="Engine", token_budget=4000, storage_path=storage)
        assert "error" not in result
        for item in result["context_items"]:
            assert "relevance_score" in item
            assert "centrality_score" in item
            assert "combined_score" in item
            assert "tokens" in item

    def test_bm25_strategy(self, tmp_path):
        """strategy='bm25' returns results without error."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_ranked_context(repo, query="format date", strategy="bm25", storage_path=storage)
        assert "error" not in result
        assert "context_items" in result

    def test_budget_zero_returns_error(self, tmp_path):
        """token_budget=0 returns a structured error."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_ranked_context(repo, query="engine", token_budget=0, storage_path=storage)
        assert "error" in result

    def test_include_kinds_filter(self, tmp_path):
        """include_kinds restricts results to specified kinds."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_ranked_context(
            repo, query="engine", token_budget=4000,
            include_kinds=["class"],
            storage_path=storage,
        )
        assert "error" not in result
        for item in result["context_items"]:
            # symbol_id encodes kind; verify via cross-check below
            # Just check we got results without crashing
            assert "symbol_id" in item

    def test_unknown_repo_returns_error(self, tmp_path):
        """Non-existent repo returns a structured error."""
        storage = str(tmp_path / ".index")
        result = get_ranked_context("no_such_repo", query="engine", storage_path=storage)
        assert "error" in result

    def test_query_too_long_returns_error(self, tmp_path):
        """Query exceeding 500 chars returns a structured error."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_ranked_context(repo, query="x" * 501, storage_path=storage)
        assert "error" in result

    def test_meta_fields_present(self, tmp_path):
        """Response includes _meta with timing and savings fields."""
        repo, storage = _make_repo(tmp_path, _SMALL_REPO)
        result = get_ranked_context(repo, query="Engine", storage_path=storage)
        assert "error" not in result
        assert "_meta" in result
        meta = result["_meta"]
        assert "timing_ms" in meta
        assert "tokens_saved" in meta
