"""Tests for get_symbol_complexity tool."""

import pytest
from jcodemunch_mcp.tools.get_symbol_complexity import get_symbol_complexity
from jcodemunch_mcp.tools.index_folder import index_folder
from jcodemunch_mcp.tools.get_file_outline import get_file_outline


def _build_repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    store.mkdir()

    (src / "funcs.py").write_text(
        "def simple():\n    return 1\n\n"
        "def branchy(x, y, z):\n"
        "    if x > 0:\n"
        "        if y > 0:\n"
        "            if z > 0:\n"
        "                return x + y + z\n"
        "            else:\n"
        "                return x + y\n"
        "        elif y < 0:\n"
        "            return x - y\n"
        "        else:\n"
        "            return x\n"
        "    elif x < 0:\n"
        "        return -x\n"
        "    else:\n"
        "        return 0\n"
    )
    r = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert r["success"] is True
    return r["repo"], str(store)


def _find_symbol_id(repo, store, name):
    result = get_file_outline(repo=repo, file_path="funcs.py", storage_path=store)
    for sym in result.get("symbols", []):
        if sym.get("name") == name:
            return sym["id"]
    return None


class TestGetSymbolComplexity:
    def test_returns_complexity_fields(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        sid = _find_symbol_id(repo, store, "branchy")
        assert sid is not None
        result = get_symbol_complexity(repo=repo, symbol_id=sid, storage_path=store)
        assert "cyclomatic" in result
        assert "max_nesting" in result
        assert "param_count" in result
        assert "assessment" in result

    def test_assessment_values(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        sid = _find_symbol_id(repo, store, "simple")
        result = get_symbol_complexity(repo=repo, symbol_id=sid, storage_path=store)
        assert result["assessment"] in ("low", "medium", "high")

    def test_branchy_higher_than_simple(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        sid_simple = _find_symbol_id(repo, store, "simple")
        sid_branchy = _find_symbol_id(repo, store, "branchy")
        r_simple = get_symbol_complexity(repo=repo, symbol_id=sid_simple, storage_path=store)
        r_branchy = get_symbol_complexity(repo=repo, symbol_id=sid_branchy, storage_path=store)
        assert r_branchy["cyclomatic"] >= r_simple["cyclomatic"]

    def test_param_count_for_branchy(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        sid = _find_symbol_id(repo, store, "branchy")
        result = get_symbol_complexity(repo=repo, symbol_id=sid, storage_path=store)
        assert result["param_count"] == 3

    def test_missing_repo_returns_error(self, tmp_path):
        result = get_symbol_complexity(
            repo="no_such_repo", symbol_id="x", storage_path=str(tmp_path)
        )
        assert "error" in result

    def test_missing_symbol_returns_error(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        result = get_symbol_complexity(
            repo=repo, symbol_id="nonexistent::symbol", storage_path=store
        )
        assert "error" in result

    def test_timing_present(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        sid = _find_symbol_id(repo, store, "simple")
        result = get_symbol_complexity(repo=repo, symbol_id=sid, storage_path=store)
        assert "_meta" in result
        assert "timing_ms" in result["_meta"]

    def test_repo_field_present(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        sid = _find_symbol_id(repo, store, "simple")
        result = get_symbol_complexity(repo=repo, symbol_id=sid, storage_path=store)
        assert "repo" in result
        assert "/" in result["repo"]

    def test_file_and_line_fields(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        sid = _find_symbol_id(repo, store, "simple")
        result = get_symbol_complexity(repo=repo, symbol_id=sid, storage_path=store)
        assert "file" in result
        assert "line" in result
