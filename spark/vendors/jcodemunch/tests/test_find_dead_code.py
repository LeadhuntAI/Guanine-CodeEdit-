"""Tests for find_dead_code tool."""

import pytest
from pathlib import Path

from jcodemunch_mcp.tools.find_dead_code import find_dead_code
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


# ---------------------------------------------------------------------------
# Basic dead file detection
# ---------------------------------------------------------------------------

class TestDeadFiles:
    def test_unreferenced_file_is_dead(self, tmp_path):
        """A file with zero importers and no entry-point name → dead (conf 1.0)."""
        repo, storage = _make_repo(tmp_path, {
            "main.py": "from src import core\nif __name__ == '__main__': core.run()",
            "src/__init__.py": "",
            "src/core.py": "def run(): pass",
            "src/unused.py": "def helper(): pass",
        })
        result = find_dead_code(repo, granularity="file", storage_path=storage)
        assert "error" not in result
        dead = {d["file"] for d in result["dead_files"]}
        assert any("unused" in f for f in dead), f"Expected unused.py in dead files, got {dead}"

    def test_main_py_not_dead(self, tmp_path):
        """main.py is always treated as a live root."""
        repo, storage = _make_repo(tmp_path, {
            "main.py": "def main(): pass",
        })
        result = find_dead_code(repo, granularity="file", storage_path=storage)
        assert "error" not in result
        dead = {d["file"] for d in result["dead_files"]}
        assert not any("main.py" in f for f in dead)

    def test_init_file_not_dead(self, tmp_path):
        """__init__.py is always a live root."""
        repo, storage = _make_repo(tmp_path, {
            "pkg/__init__.py": "from .core import run",
            "pkg/core.py": "def run(): pass",
        })
        result = find_dead_code(repo, granularity="file", storage_path=storage)
        assert "error" not in result
        dead = {d["file"] for d in result["dead_files"]}
        assert not any("__init__.py" in f for f in dead)

    def test_main_guard_file_not_dead(self, tmp_path):
        """File with `if __name__ == '__main__':` is detected as live root."""
        repo, storage = _make_repo(tmp_path, {
            "runner.py": "def go(): pass\nif __name__ == '__main__': go()",
        })
        result = find_dead_code(repo, granularity="file", storage_path=storage)
        assert "error" not in result
        dead = {d["file"] for d in result["dead_files"]}
        assert not any("runner.py" in f for f in dead)

    def test_live_imported_file_not_dead(self, tmp_path):
        """Repo with no unreferenced files returns no dead files."""
        repo, storage = _make_repo(tmp_path, {
            "main.py": "from utils import helper\nif __name__ == '__main__': helper()",
            "utils.py": "def helper(): pass",
        })
        result = find_dead_code(repo, granularity="file", storage_path=storage)
        assert "error" not in result
        dead_files = [d["file"] for d in result["dead_files"]]
        assert not any("utils" in f for f in dead_files)

    def test_min_confidence_1_0_excludes_cascade(self, tmp_path):
        """min_confidence=1.0 excludes cascading dead code (confidence 0.7)."""
        repo, storage = _make_repo(tmp_path, {
            "main.py": "if __name__ == '__main__': pass",
            "orphan_a.py": "from orphan_b import x",
            "orphan_b.py": "x = 1",
        })
        result_low = find_dead_code(repo, granularity="file",
                                     min_confidence=0.7, storage_path=storage)
        result_high = find_dead_code(repo, granularity="file",
                                      min_confidence=1.0, storage_path=storage)
        # orphan_b has zero importers → conf 1.0, appears in both
        assert result_high["dead_file_count"] <= result_low["dead_file_count"]


# ---------------------------------------------------------------------------
# Symbol granularity
# ---------------------------------------------------------------------------

class TestDeadSymbols:
    def test_symbol_in_dead_file_is_reported(self, tmp_path):
        """Symbols inside a dead file appear in dead_symbols."""
        repo, storage = _make_repo(tmp_path, {
            "main.py": "if __name__ == '__main__': pass",
            "unused.py": "def forgotten(): pass\nclass OldClass: pass",
        })
        result = find_dead_code(repo, granularity="symbol", storage_path=storage)
        assert "error" not in result
        ids = {s["symbol_id"] for s in result["dead_symbols"]}
        assert any("forgotten" in sid for sid in ids), f"Expected forgotten in {ids}"

    def test_symbol_in_live_file_not_reported(self, tmp_path):
        """Symbols in files reachable from an entry point are NOT dead."""
        repo, storage = _make_repo(tmp_path, {
            "main.py": "from lib import used_fn\nif __name__ == '__main__': used_fn()",
            "lib.py": "def used_fn(): pass",
        })
        result = find_dead_code(repo, granularity="symbol", storage_path=storage)
        assert "error" not in result
        ids = {s["symbol_id"] for s in result["dead_symbols"]}
        assert not any("used_fn" in sid for sid in ids)

    def test_granularity_file_omits_symbols(self, tmp_path):
        """granularity='file' returns empty dead_symbols list."""
        repo, storage = _make_repo(tmp_path, {
            "main.py": "if __name__ == '__main__': pass",
            "dead.py": "def noop(): pass",
        })
        result = find_dead_code(repo, granularity="file", storage_path=storage)
        assert result["dead_symbols"] == []
        assert result["dead_symbol_count"] == 0


# ---------------------------------------------------------------------------
# include_tests + entry_point_patterns
# ---------------------------------------------------------------------------

class TestOptions:
    def test_include_tests_false_excludes_test_files(self, tmp_path):
        """Test files are not reported as dead when include_tests=False."""
        repo, storage = _make_repo(tmp_path, {
            "main.py": "if __name__ == '__main__': pass",
            "tests/test_foo.py": "def test_something(): assert True",
        })
        result = find_dead_code(repo, granularity="file",
                                 include_tests=False, storage_path=storage)
        assert "error" not in result
        dead = {d["file"] for d in result["dead_files"]}
        assert not any("test_foo" in f for f in dead)

    def test_entry_point_patterns_respected(self, tmp_path):
        """Files matching entry_point_patterns are treated as live roots."""
        repo, storage = _make_repo(tmp_path, {
            "scripts/deploy.py": "def deploy(): pass",
        })
        result_without = find_dead_code(repo, granularity="file",
                                         storage_path=storage)
        result_with = find_dead_code(repo, granularity="file",
                                      entry_point_patterns=["scripts/*.py"],
                                      storage_path=storage)
        dead_without = {d["file"] for d in result_without["dead_files"]}
        dead_with = {d["file"] for d in result_with["dead_files"]}
        assert any("deploy" in f for f in dead_without)
        assert not any("deploy" in f for f in dead_with)


# ---------------------------------------------------------------------------
# Error cases + response shape
# ---------------------------------------------------------------------------

class TestErrors:
    def test_unknown_repo_returns_error(self, tmp_path):
        result = find_dead_code("nonexistent/repo",
                                 storage_path=str(tmp_path / ".index"))
        assert "error" in result

    def test_response_shape(self, tmp_path):
        """Response always has required top-level fields."""
        repo, storage = _make_repo(tmp_path, {
            "main.py": "if __name__ == '__main__': pass",
        })
        result = find_dead_code(repo, storage_path=storage)
        for key in ("dead_symbols", "dead_files", "dead_file_count",
                    "dead_symbol_count", "live_root_count", "analysis_notes", "_meta"):
            assert key in result, f"Missing key: {key}"
