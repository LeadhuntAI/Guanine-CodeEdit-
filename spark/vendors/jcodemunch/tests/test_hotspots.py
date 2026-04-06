"""Tests for get_hotspots tool."""

import subprocess
import pytest
from jcodemunch_mcp.tools.get_hotspots import get_hotspots
from jcodemunch_mcp.tools.index_folder import index_folder


def _build_repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (src / "a.py").write_text(
        "def simple():\n    return 1\n\n"
        "def branchy(x, y):\n"
        "    if x > 0:\n"
        "        if y > 0:\n"
        "            return x + y\n"
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


def _build_git_repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    (src / "a.py").write_text(
        "def simple():\n    return 1\n\n"
        "def branchy(x, y):\n"
        "    if x:\n        return x\n    return 0\n"
    )
    subprocess.run(["git", "init"], cwd=str(src), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(src), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(src), capture_output=True)
    subprocess.run(["git", "add", "."], cwd=str(src), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(src), capture_output=True)
    r = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert r["success"] is True
    return r["repo"], str(store)


class TestGetHotspots:
    def test_returns_hotspots_list(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        result = get_hotspots(repo=repo, storage_path=store)
        assert "hotspots" in result
        assert isinstance(result["hotspots"], list)

    def test_hotspot_fields(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        result = get_hotspots(repo=repo, min_complexity=1, storage_path=store)
        for h in result["hotspots"]:
            assert "symbol_id" in h
            assert "name" in h
            assert "file" in h
            assert "cyclomatic" in h
            assert "churn" in h
            assert "hotspot_score" in h

    def test_top_n_respected(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        result = get_hotspots(repo=repo, top_n=1, min_complexity=1, storage_path=store)
        assert len(result["hotspots"]) <= 1

    def test_sorted_by_score_desc(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        result = get_hotspots(repo=repo, min_complexity=1, storage_path=store)
        scores = [h["hotspot_score"] for h in result["hotspots"]]
        assert scores == sorted(scores, reverse=True)

    def test_min_complexity_filter(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        result_all = get_hotspots(repo=repo, min_complexity=1, storage_path=store)
        result_high = get_hotspots(repo=repo, min_complexity=20, storage_path=store)
        assert len(result_high["hotspots"]) <= len(result_all["hotspots"])

    def test_missing_repo_returns_error(self, tmp_path):
        result = get_hotspots(repo="no_such_repo", storage_path=str(tmp_path))
        assert "error" in result

    def test_timing_present(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        result = get_hotspots(repo=repo, storage_path=store)
        assert "_meta" in result
        assert "timing_ms" in result["_meta"]

    def test_git_available_field(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        result = get_hotspots(repo=repo, storage_path=store)
        assert "git_available" in result

    def test_with_git_repo(self, tmp_path):
        try:
            repo, store = _build_git_repo(tmp_path)
        except Exception:
            pytest.skip("git not available")
        result = get_hotspots(repo=repo, min_complexity=1, storage_path=store)
        assert "hotspots" in result
        if result["git_available"]:
            # Any symbol touched in the commit should have churn >= 0
            for h in result["hotspots"]:
                assert h["churn"] >= 0

    def test_assessment_field_present(self, tmp_path):
        repo, store = _build_repo(tmp_path)
        result = get_hotspots(repo=repo, min_complexity=1, storage_path=store)
        for h in result["hotspots"]:
            assert "assessment" in h
            assert h["assessment"] in ("low", "medium", "high")

    def test_high_complexity_no_churn_is_low(self, tmp_path):
        # Without git history, churn=0 so hotspot_score = cyclomatic * log(1) = 0
        # All symbols should be "low" assessment
        repo, store = _build_repo(tmp_path)
        result = get_hotspots(repo=repo, min_complexity=1, storage_path=store)
        if not result.get("git_available"):
            for h in result["hotspots"]:
                assert h["assessment"] == "low"
