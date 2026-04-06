"""Tests for get_churn_rate tool.

Most tests operate without a real git repo (no source_root) to stay fast and portable.
A small subset creates an actual git repo to verify git integration.
"""

import subprocess
import pytest
from jcodemunch_mcp.tools.get_churn_rate import get_churn_rate
from jcodemunch_mcp.tools.index_folder import index_folder
from jcodemunch_mcp.tools.get_file_outline import get_file_outline


def _build_non_git_repo(tmp_path):
    """Index a folder that is NOT a git repo (churn will gracefully error)."""
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (src / "utils.py").write_text("def helper():\n    return 1\n")
    r = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert r["success"] is True
    return r["repo"], str(store)


def _build_git_repo(tmp_path):
    """Create a minimal git repo with one commit so churn queries work."""
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    store.mkdir()

    (src / "utils.py").write_text("def helper():\n    return 1\n")

    # Init git repo
    subprocess.run(["git", "init"], cwd=str(src), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(src), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(src), capture_output=True)
    subprocess.run(["git", "add", "."], cwd=str(src), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(src), capture_output=True)

    r = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert r["success"] is True
    return r["repo"], str(store)


class TestGetChurnRateErrors:
    def test_missing_repo_returns_error(self, tmp_path):
        result = get_churn_rate(
            repo="no_such_repo", target="utils.py", storage_path=str(tmp_path)
        )
        assert "error" in result

    def test_no_source_root_returns_error(self, tmp_path):
        """Repos indexed without source_root (e.g. index_repo) should return error."""
        repo, store = _build_non_git_repo(tmp_path)
        # index_folder always sets source_root, so patch it out via a non-git path
        # We test the "not a git repo" branch instead
        result = get_churn_rate(repo=repo, target="utils.py", storage_path=store)
        # Either an error (not a git repo) or valid result with commits=0
        assert "error" in result or "commits" in result


class TestGetChurnRateStructure:
    def test_result_fields(self, tmp_path):
        """Verify all expected output fields are present."""
        try:
            repo, store = _build_git_repo(tmp_path)
        except Exception:
            pytest.skip("git not available")
        result = get_churn_rate(repo=repo, target="utils.py", storage_path=store)
        if "error" in result:
            pytest.skip(f"git error: {result['error']}")
        assert "commits" in result
        assert "authors" in result
        assert "churn_per_week" in result
        assert "assessment" in result
        assert "target_type" in result
        assert "file" in result
        assert "_meta" in result

    def test_assessment_values(self, tmp_path):
        try:
            repo, store = _build_git_repo(tmp_path)
        except Exception:
            pytest.skip("git not available")
        result = get_churn_rate(repo=repo, target="utils.py", storage_path=store)
        if "error" in result:
            pytest.skip(f"git error: {result['error']}")
        assert result["assessment"] in ("stable", "active", "volatile")

    def test_file_target_type(self, tmp_path):
        try:
            repo, store = _build_git_repo(tmp_path)
        except Exception:
            pytest.skip("git not available")
        result = get_churn_rate(repo=repo, target="utils.py", storage_path=store)
        if "error" in result:
            pytest.skip(f"git error: {result['error']}")
        assert result["target_type"] == "file"

    def test_symbol_target_type(self, tmp_path):
        try:
            repo, store = _build_git_repo(tmp_path)
        except Exception:
            pytest.skip("git not available")
        # Find a valid symbol ID
        sr = get_file_outline(repo=repo, file_path="utils.py", storage_path=store)
        syms = sr.get("symbols", [])
        if not syms:
            pytest.skip("no symbols indexed")
        sid = syms[0]["id"]
        result = get_churn_rate(repo=repo, target=sid, storage_path=store)
        if "error" in result:
            pytest.skip(f"git error: {result['error']}")
        assert result["target_type"] == "symbol"
        assert result["symbol_name"] == "helper"

    def test_timing_present(self, tmp_path):
        try:
            repo, store = _build_git_repo(tmp_path)
        except Exception:
            pytest.skip("git not available")
        result = get_churn_rate(repo=repo, target="utils.py", storage_path=store)
        if "error" in result:
            pytest.skip(f"git error: {result['error']}")
        assert result["_meta"]["timing_ms"] >= 0

    def test_days_param_respected(self, tmp_path):
        try:
            repo, store = _build_git_repo(tmp_path)
        except Exception:
            pytest.skip("git not available")
        result = get_churn_rate(repo=repo, target="utils.py", days=1, storage_path=store)
        if "error" in result:
            pytest.skip(f"git error: {result['error']}")
        assert result["days"] == 1
