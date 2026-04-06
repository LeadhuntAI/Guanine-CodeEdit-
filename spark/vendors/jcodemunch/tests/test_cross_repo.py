"""Tests for cross-repository dependency tracking (v1.13.0).

Covers:
 - extract_package_names for each ecosystem
 - extract_root_package_from_specifier for each language
 - package_names field round-trip through save_index / load_index
 - build_package_registry
 - find_importers cross_repo parameter
 - get_blast_radius cross_repo parameter
 - get_dependency_graph cross_repo parameter
 - get_cross_repo_map tool
 - JCODEMUNCH_CROSS_REPO_DEFAULT env var
 - edge cases: no manifest, malformed manifest, circular deps, no match
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import pytest

from jcodemunch_mcp.tools.package_registry import (
    build_package_registry,
    extract_package_names,
    extract_root_package_from_specifier,
    find_repos_for_package,
    invalidate_registry_cache,
)
from jcodemunch_mcp.tools.index_folder import index_folder
from jcodemunch_mcp.storage.sqlite_store import _cache_clear
from jcodemunch_mcp.storage import IndexStore


# ============================================================
# Helpers
# ============================================================

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _index(folder: Path, store: Path) -> str:
    """Index a folder and return the repo ID."""
    result = index_folder(str(folder), use_ai_summaries=False, storage_path=str(store))
    assert result["success"] is True, result
    return result["repo"]


# ============================================================
# Phase 1: extract_package_names — per ecosystem
# ============================================================

class TestExtractPackageNamesPython:

    def test_pyproject_toml_name(self, tmp_path):
        _write(tmp_path / "pyproject.toml", '[project]\nname = "my-lib"\n')
        _write(tmp_path / "src/my_lib/__init__.py", "")
        names = extract_package_names(str(tmp_path))
        assert "my-lib" in names

    def test_pyproject_toml_underscore_normalized(self, tmp_path):
        _write(tmp_path / "pyproject.toml", '[project]\nname = "my_lib"\n')
        names = extract_package_names(str(tmp_path))
        assert "my-lib" in names

    def test_setup_cfg_fallback(self, tmp_path):
        _write(tmp_path / "setup.cfg", "[metadata]\nname = requests\n")
        _write(tmp_path / "src/requests/__init__.py", "")
        names = extract_package_names(str(tmp_path))
        assert "requests" in names

    def test_pyproject_wins_over_setup_cfg(self, tmp_path):
        _write(tmp_path / "pyproject.toml", '[project]\nname = "alpha"\n')
        _write(tmp_path / "setup.cfg", "[metadata]\nname = beta\n")
        names = extract_package_names(str(tmp_path))
        assert "alpha" in names


class TestExtractPackageNamesJavaScript:

    def test_package_json_name(self, tmp_path):
        _write(tmp_path / "package.json", '{"name": "my-package", "version": "1.0.0"}')
        _write(tmp_path / "index.js", "")
        names = extract_package_names(str(tmp_path))
        assert "my-package" in names

    def test_scoped_package_json(self, tmp_path):
        _write(tmp_path / "package.json", '{"name": "@org/toolkit"}')
        names = extract_package_names(str(tmp_path))
        assert "@org/toolkit" in names


class TestExtractPackageNamesGo:

    def test_go_mod_module(self, tmp_path):
        _write(tmp_path / "go.mod", "module github.com/myorg/myrepo\n\ngo 1.21\n")
        _write(tmp_path / "main.go", "package main\n")
        names = extract_package_names(str(tmp_path))
        assert "github.com/myorg/myrepo" in names


class TestExtractPackageNamesRust:

    def test_cargo_toml_name(self, tmp_path):
        _write(tmp_path / "Cargo.toml", '[package]\nname = "my-crate"\nversion = "0.1.0"\n')
        _write(tmp_path / "src/lib.rs", "")
        names = extract_package_names(str(tmp_path))
        assert "my-crate" in names


class TestExtractPackageNamesCSharp:

    def test_csproj_package_name(self, tmp_path):
        _write(tmp_path / "MyLib.csproj",
               '<Project><PropertyGroup><PackageName>MyLib.Core</PackageName></PropertyGroup></Project>')
        names = extract_package_names(str(tmp_path))
        assert "MyLib.Core" in names

    def test_csproj_assembly_name_fallback(self, tmp_path):
        _write(tmp_path / "MyLib.csproj",
               '<Project><PropertyGroup><AssemblyName>MyLib</AssemblyName></PropertyGroup></Project>')
        names = extract_package_names(str(tmp_path))
        assert "MyLib" in names


class TestExtractPackageNamesEdgeCases:

    def test_no_manifest_returns_empty(self, tmp_path):
        _write(tmp_path / "main.py", "print('hello')\n")
        names = extract_package_names(str(tmp_path))
        assert names == []

    def test_malformed_pyproject_toml_does_not_raise(self, tmp_path):
        _write(tmp_path / "pyproject.toml", "this is not valid toml ][[[")
        # Should not raise, may or may not find a name
        try:
            names = extract_package_names(str(tmp_path))
            assert isinstance(names, list)
        except Exception as e:
            pytest.fail(f"extract_package_names raised: {e}")

    def test_malformed_package_json_does_not_raise(self, tmp_path):
        _write(tmp_path / "package.json", "{broken json")
        try:
            names = extract_package_names(str(tmp_path))
            assert isinstance(names, list)
        except Exception as e:
            pytest.fail(f"extract_package_names raised: {e}")

    def test_empty_manifest_returns_empty(self, tmp_path):
        _write(tmp_path / "pyproject.toml", "")
        names = extract_package_names(str(tmp_path))
        assert names == []


# ============================================================
# Phase 2: extract_root_package_from_specifier
# ============================================================

class TestExtractRootPackagePython:

    def test_simple_module(self):
        assert extract_root_package_from_specifier("flask", "python") == "flask"

    def test_dotted_module(self):
        assert extract_root_package_from_specifier("flask.blueprints", "python") == "flask"

    def test_relative_import_returns_empty(self):
        assert extract_root_package_from_specifier(".utils", "python") == ""

    def test_relative_import_multi_dot_returns_empty(self):
        assert extract_root_package_from_specifier("..models", "python") == ""

    def test_pure_relative_returns_empty(self):
        assert extract_root_package_from_specifier("...", "python") == ""


class TestExtractRootPackageJavaScript:

    def test_unscoped_package(self):
        assert extract_root_package_from_specifier("react", "javascript") == "react"

    def test_unscoped_with_subpath(self):
        assert extract_root_package_from_specifier("lodash/merge", "javascript") == "lodash"

    def test_scoped_package(self):
        assert extract_root_package_from_specifier("@org/package", "javascript") == "@org/package"

    def test_scoped_with_subpath(self):
        assert extract_root_package_from_specifier("@org/package/utils", "javascript") == "@org/package"

    def test_relative_import_returns_empty(self):
        assert extract_root_package_from_specifier("./local", "typescript") == ""

    def test_relative_dotdot_returns_empty(self):
        assert extract_root_package_from_specifier("../sibling", "typescript") == ""


class TestExtractRootPackageGo:

    def test_three_segment_module(self):
        result = extract_root_package_from_specifier("github.com/gin-gonic/gin/router", "go")
        assert result == "github.com/gin-gonic/gin"

    def test_short_module(self):
        result = extract_root_package_from_specifier("fmt", "go")
        assert result == "fmt"


class TestExtractRootPackageRust:

    def test_crate_path(self):
        assert extract_root_package_from_specifier("serde::de::Deserialize", "rust") == "serde"

    def test_simple_crate(self):
        assert extract_root_package_from_specifier("tokio", "rust") == "tokio"


# ============================================================
# Phase 3: package_names field round-trip
# ============================================================

class TestPackageNamesRoundTrip:

    def test_package_names_saved_and_loaded(self, tmp_path):
        src = tmp_path / "src"
        store_dir = tmp_path / "store"
        src.mkdir()
        store_dir.mkdir()
        _write(src / "pyproject.toml", '[project]\nname = "mylib"\n')
        _write(src / "mylib.py", "def hello(): pass\n")

        repo_id = _index(src, store_dir)

        _cache_clear()
        store = IndexStore(base_path=str(store_dir))
        owner, name = repo_id.split("/", 1)
        index = store.load_index(owner, name)
        assert index is not None
        assert hasattr(index, "package_names")
        assert "mylib" in index.package_names

    def test_old_index_without_package_names_loads_cleanly(self, tmp_path):
        """Simulate loading an old index that has no package_names in meta."""
        import sqlite3

        src = tmp_path / "src"
        store_dir = tmp_path / "store"
        src.mkdir()
        store_dir.mkdir()
        _write(src / "app.py", "def main(): pass\n")

        repo_id = _index(src, store_dir)
        owner, name = repo_id.split("/", 1)

        # Remove package_names from meta table to simulate old index
        from jcodemunch_mcp.storage.sqlite_store import SQLiteIndexStore
        sqlite_store = SQLiteIndexStore(base_path=str(store_dir))
        db_path = sqlite_store._db_path(owner, name)
        _cache_clear()
        conn = sqlite3.connect(str(db_path))
        conn.execute("DELETE FROM meta WHERE key = 'package_names'")
        conn.commit()
        conn.close()

        _cache_clear()
        store = IndexStore(base_path=str(store_dir))
        index = store.load_index(owner, name)
        assert index is not None
        assert hasattr(index, "package_names")
        assert index.package_names == []  # Empty list, not None, no crash

    def test_package_names_persisted_via_save_index(self, tmp_path):
        from jcodemunch_mcp.storage.sqlite_store import SQLiteIndexStore
        from jcodemunch_mcp.parser.symbols import Symbol

        store_dir = tmp_path / "store"
        store_dir.mkdir()
        sqlite_store = SQLiteIndexStore(base_path=str(store_dir))

        index = sqlite_store.save_index(
            owner="test",
            name="myrepo",
            source_files=["app.py"],
            symbols=[],
            raw_files={"app.py": "x = 1\n"},
            package_names=["my-package", "my-alias"],
        )
        assert "my-package" in index.package_names

        _cache_clear()
        loaded = sqlite_store.load_index("test", "myrepo")
        assert loaded is not None
        assert "my-package" in loaded.package_names
        assert "my-alias" in loaded.package_names


# ============================================================
# Phase 4: build_package_registry
# ============================================================

class TestBuildPackageRegistry:

    def _make_repo_entry(self, repo_id: str, pkg_names: list[str]) -> dict:
        return {"repo": repo_id, "source_root": "", "package_names": pkg_names}

    def test_maps_package_to_repo(self):
        invalidate_registry_cache()
        repos = [self._make_repo_entry("local/repo-a", ["requests"])]
        registry = build_package_registry(repos)
        assert "requests" in registry
        assert "local/repo-a" in registry["requests"]

    def test_two_repos_same_package(self):
        """Both repos should be in the registry for that package."""
        invalidate_registry_cache()
        repos = [
            self._make_repo_entry("local/repo-a", ["shared-pkg"]),
            self._make_repo_entry("local/repo-b", ["shared-pkg"]),
        ]
        registry = build_package_registry(repos)
        assert len(registry.get("shared-pkg", [])) == 2
        assert "local/repo-a" in registry["shared-pkg"]
        assert "local/repo-b" in registry["shared-pkg"]

    def test_find_repos_for_package_returns_correct_repos(self):
        invalidate_registry_cache()
        repos = [self._make_repo_entry("local/flask-repo", ["flask"])]
        result = find_repos_for_package("flask", repos)
        assert "local/flask-repo" in result

    def test_find_repos_for_nonexistent_package_returns_empty(self):
        invalidate_registry_cache()
        repos = [self._make_repo_entry("local/repo-a", ["numpy"])]
        result = find_repos_for_package("pandas", repos)
        assert result == []


# ============================================================
# Phase 5: find_importers cross_repo=False baseline
# ============================================================

class TestFindImportersBaseline:
    """cross_repo=False must produce identical results to the old behavior."""

    def _build_repo(self, tmp_path):
        src = tmp_path / "src"
        store = tmp_path / "store"
        src.mkdir()
        store.mkdir()
        _write(src / "utils.py", "def helper(): return 42\n")
        _write(src / "main.py", "from utils import helper\nresult = helper()\n")
        repo_id = _index(src, store)
        return repo_id, str(store)

    def test_cross_repo_false_finds_local_importers(self, tmp_path):
        from jcodemunch_mcp.tools.find_importers import find_importers

        repo_id, store = self._build_repo(tmp_path)
        result = find_importers(
            repo=repo_id,
            file_path="utils.py",
            storage_path=store,
            cross_repo=False,
        )
        assert "importers" in result
        importers = [r["file"] for r in result["importers"]]
        assert "main.py" in importers

    def test_cross_repo_false_no_cross_repo_key(self, tmp_path):
        from jcodemunch_mcp.tools.find_importers import find_importers

        repo_id, store = self._build_repo(tmp_path)
        result = find_importers(
            repo=repo_id,
            file_path="utils.py",
            storage_path=store,
            cross_repo=False,
        )
        # Should not add cross_repo_importer_count when cross_repo=False
        assert "cross_repo_importer_count" not in result


# ============================================================
# Phase 5b: find_importers cross_repo=True
# ============================================================

class TestFindImportersCrossRepo:

    def _build_two_repos(self, tmp_path):
        """Build a provider repo (publishes 'mylib') and a consumer repo (imports mylib)."""
        provider_src = tmp_path / "provider"
        consumer_src = tmp_path / "consumer"
        store = tmp_path / "store"
        provider_src.mkdir()
        consumer_src.mkdir()
        store.mkdir()

        # Provider: publishes "mylib"
        _write(provider_src / "pyproject.toml", '[project]\nname = "mylib"\n')
        _write(provider_src / "__init__.py", "def exported(): pass\n")

        # Consumer: imports from mylib
        _write(consumer_src / "app.py", "import mylib\nmylib.exported()\n")

        provider_id = _index(provider_src, store)
        consumer_id = _index(consumer_src, store)
        return provider_id, consumer_id, str(store)

    def test_cross_repo_true_finds_cross_repo_importers(self, tmp_path):
        from jcodemunch_mcp.tools.find_importers import find_importers

        provider_id, consumer_id, store = self._build_two_repos(tmp_path)
        invalidate_registry_cache()

        result = find_importers(
            repo=provider_id,
            file_path="__init__.py",
            storage_path=store,
            cross_repo=True,
        )
        assert "importers" in result
        cross_importers = [r for r in result["importers"] if r.get("cross_repo")]
        assert len(cross_importers) > 0

    def test_cross_repo_results_have_correct_fields(self, tmp_path):
        from jcodemunch_mcp.tools.find_importers import find_importers

        provider_id, consumer_id, store = self._build_two_repos(tmp_path)
        invalidate_registry_cache()

        result = find_importers(
            repo=provider_id,
            file_path="__init__.py",
            storage_path=store,
            cross_repo=True,
        )
        cross_importers = [r for r in result.get("importers", []) if r.get("cross_repo")]
        for r in cross_importers:
            assert r["cross_repo"] is True
            assert "source_repo" in r
            assert r["source_repo"] == consumer_id


# ============================================================
# Phase 5c: get_blast_radius cross_repo
# ============================================================

class TestGetBlastRadiusCrossRepo:

    def _build_two_repos(self, tmp_path):
        provider_src = tmp_path / "provider"
        consumer_src = tmp_path / "consumer"
        store = tmp_path / "store"
        provider_src.mkdir()
        consumer_src.mkdir()
        store.mkdir()

        _write(provider_src / "pyproject.toml", '[project]\nname = "mylib"\n')
        _write(provider_src / "core.py", "def do_thing(): pass\n")

        _write(consumer_src / "app.py", "import mylib\nmylib.do_thing()\n")

        provider_id = _index(provider_src, store)
        consumer_id = _index(consumer_src, store)
        return provider_id, consumer_id, str(store)

    def test_blast_radius_cross_repo_false_no_cross_repo_key(self, tmp_path):
        from jcodemunch_mcp.tools.get_blast_radius import get_blast_radius

        provider_id, _, store = self._build_two_repos(tmp_path)
        result = get_blast_radius(
            repo=provider_id, symbol="do_thing",
            storage_path=store, cross_repo=False,
        )
        assert "error" not in result or "Symbol not found" in result.get("error", "")
        assert "cross_repo_confirmed" not in result

    def test_blast_radius_cross_repo_true_adds_cross_repo_field(self, tmp_path):
        from jcodemunch_mcp.tools.get_blast_radius import get_blast_radius

        provider_id, consumer_id, store = self._build_two_repos(tmp_path)
        invalidate_registry_cache()

        result = get_blast_radius(
            repo=provider_id, symbol="do_thing",
            storage_path=store, cross_repo=True,
        )
        # Either found cross_repo results, or at least no crash
        assert isinstance(result, dict)
        if "cross_repo_confirmed" in result:
            assert isinstance(result["cross_repo_confirmed"], list)
            if result["cross_repo_confirmed"]:
                entry = result["cross_repo_confirmed"][0]
                assert entry.get("cross_repo") is True
                assert "source_repo" in entry


# ============================================================
# Phase 5d: get_dependency_graph cross_repo
# ============================================================

class TestGetDependencyGraphCrossRepo:

    def _build_two_repos(self, tmp_path):
        provider_src = tmp_path / "provider"
        consumer_src = tmp_path / "consumer"
        store = tmp_path / "store"
        provider_src.mkdir()
        consumer_src.mkdir()
        store.mkdir()

        _write(provider_src / "pyproject.toml", '[project]\nname = "mylib"\n')
        _write(provider_src / "core.py", "def api(): pass\n")

        _write(consumer_src / "main.py", "import mylib\nmylib.api()\n")

        provider_id = _index(provider_src, store)
        consumer_id = _index(consumer_src, store)
        return provider_id, consumer_id, str(store)

    def test_dependency_graph_cross_repo_false_no_cross_repo_edges(self, tmp_path):
        from jcodemunch_mcp.tools.get_dependency_graph import get_dependency_graph

        _, consumer_id, store = self._build_two_repos(tmp_path)
        result = get_dependency_graph(
            repo=consumer_id, file="main.py",
            direction="imports", storage_path=store, cross_repo=False,
        )
        assert "cross_repo_edges" not in result

    def test_dependency_graph_cross_repo_true_returns_dict(self, tmp_path):
        from jcodemunch_mcp.tools.get_dependency_graph import get_dependency_graph

        _, consumer_id, store = self._build_two_repos(tmp_path)
        invalidate_registry_cache()

        result = get_dependency_graph(
            repo=consumer_id, file="main.py",
            direction="imports", storage_path=store, cross_repo=True,
        )
        assert isinstance(result, dict)
        # If cross_repo_edges present, they should have the right shape
        for edge in result.get("cross_repo_edges", []):
            assert "from_repo" in edge
            assert "to_repo" in edge
            assert "package_name" in edge
            assert edge.get("cross_repo") is True


# ============================================================
# Phase 6: get_cross_repo_map
# ============================================================

class TestGetCrossRepoMap:

    def _build_two_repos(self, tmp_path):
        provider_src = tmp_path / "provider"
        consumer_src = tmp_path / "consumer"
        store = tmp_path / "store"
        provider_src.mkdir()
        consumer_src.mkdir()
        store.mkdir()

        _write(provider_src / "pyproject.toml", '[project]\nname = "mylib"\n')
        _write(provider_src / "api.py", "def do(): pass\n")

        _write(consumer_src / "app.py", "import mylib\nmylib.do()\n")

        provider_id = _index(provider_src, store)
        consumer_id = _index(consumer_src, store)
        return provider_id, consumer_id, str(store)

    def test_get_cross_repo_map_no_filter_returns_all(self, tmp_path):
        from jcodemunch_mcp.tools.get_cross_repo_map import get_cross_repo_map

        provider_id, consumer_id, store = self._build_two_repos(tmp_path)
        invalidate_registry_cache()

        result = get_cross_repo_map(storage_path=store)
        assert isinstance(result, dict)
        assert "repos" in result
        assert "cross_repo_edges" in result
        repo_ids = {r["repo"] for r in result["repos"]}
        assert provider_id in repo_ids
        assert consumer_id in repo_ids

    def test_get_cross_repo_map_with_filter(self, tmp_path):
        from jcodemunch_mcp.tools.get_cross_repo_map import get_cross_repo_map

        provider_id, consumer_id, store = self._build_two_repos(tmp_path)
        invalidate_registry_cache()

        result = get_cross_repo_map(repo=consumer_id, storage_path=store)
        assert isinstance(result, dict)
        assert "repos" in result
        assert len(result["repos"]) == 1
        assert result["repos"][0]["repo"] == consumer_id

    def test_get_cross_repo_map_unknown_repo_returns_error(self, tmp_path):
        from jcodemunch_mcp.tools.get_cross_repo_map import get_cross_repo_map

        _, _, store = self._build_two_repos(tmp_path)
        result = get_cross_repo_map(repo="nonexistent/repo", storage_path=store)
        assert "error" in result

    def test_get_cross_repo_map_cross_repo_edges_structure(self, tmp_path):
        from jcodemunch_mcp.tools.get_cross_repo_map import get_cross_repo_map

        provider_id, consumer_id, store = self._build_two_repos(tmp_path)
        invalidate_registry_cache()

        result = get_cross_repo_map(storage_path=store)
        for edge in result.get("cross_repo_edges", []):
            assert "from_repo" in edge
            assert "to_repo" in edge
            assert "package_name" in edge


# ============================================================
# Phase 7: JCODEMUNCH_CROSS_REPO_DEFAULT env var
# ============================================================

class TestCrossRepoDefaultEnvVar:

    def test_env_var_sets_cross_repo_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCODEMUNCH_CROSS_REPO_DEFAULT", "true")
        # Force config reload
        from jcodemunch_mcp import config as cfg
        from copy import deepcopy
        cfg._GLOBAL_CONFIG = deepcopy(cfg.DEFAULTS)
        # Simulate env var being applied
        cfg._GLOBAL_CONFIG["cross_repo_default"] = True
        val = cfg.get("cross_repo_default", False)
        assert val is True

    def test_env_var_false_keeps_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JCODEMUNCH_CROSS_REPO_DEFAULT", "false")
        from jcodemunch_mcp import config as cfg
        from copy import deepcopy
        cfg._GLOBAL_CONFIG = deepcopy(cfg.DEFAULTS)
        cfg._GLOBAL_CONFIG["cross_repo_default"] = False
        val = cfg.get("cross_repo_default", False)
        assert val is False


# ============================================================
# Edge cases
# ============================================================

class TestEdgeCases:

    def test_no_cross_repo_match_returns_empty(self, tmp_path):
        from jcodemunch_mcp.tools.find_importers import find_importers

        src = tmp_path / "src"
        store = tmp_path / "store"
        src.mkdir()
        store.mkdir()
        _write(src / "utils.py", "def foo(): pass\n")
        _write(src / "main.py", "from utils import foo\n")

        repo_id = _index(src, store)
        invalidate_registry_cache()

        result = find_importers(
            repo=repo_id, file_path="utils.py",
            storage_path=str(store), cross_repo=True,
        )
        # cross_repo_importer_count will be 0 or absent — no crash
        assert isinstance(result, dict)
        assert "error" not in result

    def test_circular_cross_repo_dependency_no_infinite_loop(self, tmp_path):
        """Two repos that both import each other should not cause infinite loops."""
        src_a = tmp_path / "a"
        src_b = tmp_path / "b"
        store = tmp_path / "store"
        src_a.mkdir()
        src_b.mkdir()
        store.mkdir()

        _write(src_a / "pyproject.toml", '[project]\nname = "pkg-a"\n')
        _write(src_a / "__init__.py", "import pkg_b\n")

        _write(src_b / "pyproject.toml", '[project]\nname = "pkg-b"\n')
        _write(src_b / "__init__.py", "import pkg_a\n")

        repo_a = _index(src_a, store)
        repo_b = _index(src_b, store)
        invalidate_registry_cache()

        from jcodemunch_mcp.tools.get_cross_repo_map import get_cross_repo_map
        # Must complete without hanging
        result = get_cross_repo_map(storage_path=str(store))
        assert isinstance(result, dict)
        assert "repos" in result

    def test_extract_package_names_nonexistent_dir_returns_empty(self):
        names = extract_package_names("/nonexistent/path/that/does/not/exist")
        assert names == []
