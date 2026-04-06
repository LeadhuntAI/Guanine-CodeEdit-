"""Tests for v1.15: get_dependency_cycles, get_coupling_metrics, get_layer_violations."""

import pytest
from jcodemunch_mcp.tools.index_folder import index_folder
from jcodemunch_mcp.tools.get_dependency_cycles import get_dependency_cycles, _find_cycles
from jcodemunch_mcp.tools.get_coupling_metrics import get_coupling_metrics
from jcodemunch_mcp.tools.get_layer_violations import get_layer_violations, _file_to_layer


# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------

def _build_linear_repo(tmp_path):
    """Three files with no cycles: a → b → c."""
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir(); store.mkdir()

    (src / "c.py").write_text("def leaf():\n    pass\n")
    (src / "b.py").write_text("from c import leaf\n\ndef mid():\n    return leaf()\n")
    (src / "a.py").write_text("from b import mid\n\ndef top():\n    return mid()\n")

    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert result["success"] is True
    return result["repo"], str(store), str(src)


def _build_cyclic_repo(tmp_path):
    """Two files with a mutual import cycle: x ↔ y."""
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir(); store.mkdir()

    (src / "x.py").write_text("from y import bar\n\ndef foo():\n    return bar()\n")
    (src / "y.py").write_text("from x import foo\n\ndef bar():\n    return foo()\n")
    (src / "z.py").write_text("def standalone():\n    pass\n")

    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert result["success"] is True
    return result["repo"], str(store)


def _build_layered_repo(tmp_path):
    """
    Flat repo where imports resolve via simple name matching:
        routes.py  → imports from models.py  (VIOLATION: api may_not_import db)
        routes.py  → imports from service.py (OK)
        service.py → imports from models.py  (OK)

    Layer rules use exact filename paths since _file_to_layer supports exact match.
    """
    src = tmp_path / "src"
    store = tmp_path / "store"
    src.mkdir(); store.mkdir()

    (src / "models.py").write_text("class User:\n    pass\n")
    (src / "service.py").write_text(
        "from models import User\n\ndef get_user():\n    return User()\n"
    )
    (src / "routes.py").write_text(
        "from models import User\nfrom service import get_user\n\n"
        "def handle():\n    return get_user()\n"
    )

    result = index_folder(str(src), use_ai_summaries=False, storage_path=str(store))
    assert result["success"] is True
    return result["repo"], str(store), str(src)


# ---------------------------------------------------------------------------
# _find_cycles unit tests
# ---------------------------------------------------------------------------

class TestFindCycles:

    def test_empty_graph_returns_no_cycles(self):
        assert _find_cycles({}) == []

    def test_linear_graph_no_cycle(self):
        adj = {"a": ["b"], "b": ["c"], "c": []}
        assert _find_cycles(adj) == []

    def test_self_loop_not_reported(self):
        # Self-loops are not SCC > 1 (single node)
        adj = {"a": ["a"]}
        cycles = _find_cycles(adj)
        assert cycles == []

    def test_two_node_mutual_cycle(self):
        adj = {"x": ["y"], "y": ["x"]}
        cycles = _find_cycles(adj)
        assert len(cycles) == 1
        assert set(cycles[0]) == {"x", "y"}

    def test_three_node_cycle(self):
        adj = {"a": ["b"], "b": ["c"], "c": ["a"]}
        cycles = _find_cycles(adj)
        assert len(cycles) == 1
        assert set(cycles[0]) == {"a", "b", "c"}

    def test_two_independent_cycles(self):
        adj = {
            "a": ["b"], "b": ["a"],
            "c": ["d"], "d": ["c"],
            "e": [],
        }
        cycles = _find_cycles(adj)
        assert len(cycles) == 2
        cycle_sets = [frozenset(c) for c in cycles]
        assert frozenset({"a", "b"}) in cycle_sets
        assert frozenset({"c", "d"}) in cycle_sets

    def test_isolated_node_not_in_cycle(self):
        adj = {"a": ["b"], "b": ["a"], "isolated": []}
        cycles = _find_cycles(adj)
        assert len(cycles) == 1
        assert "isolated" not in cycles[0]

    def test_cycle_members_are_sorted(self):
        adj = {"z": ["a"], "a": ["z"]}
        cycles = _find_cycles(adj)
        assert cycles[0] == sorted(cycles[0])


# ---------------------------------------------------------------------------
# get_dependency_cycles integration tests
# ---------------------------------------------------------------------------

class TestGetDependencyCycles:

    def test_no_cycles_in_linear_repo(self, tmp_path):
        repo, store, _ = _build_linear_repo(tmp_path)
        result = get_dependency_cycles(repo=repo, storage_path=store)
        assert "error" not in result, result.get("error")
        assert result["cycle_count"] == 0
        assert result["cycles"] == []

    def test_cycle_detected_in_cyclic_repo(self, tmp_path):
        repo, store = _build_cyclic_repo(tmp_path)
        result = get_dependency_cycles(repo=repo, storage_path=store)
        assert "error" not in result, result.get("error")
        assert result["cycle_count"] >= 1
        # x.py and y.py should be in the same SCC
        found = any(
            any("x.py" in f for f in cycle) and any("y.py" in f for f in cycle)
            for cycle in result["cycles"]
        )
        assert found, f"Expected x/y cycle, got: {result['cycles']}"

    def test_standalone_file_not_in_cycle(self, tmp_path):
        repo, store = _build_cyclic_repo(tmp_path)
        result = get_dependency_cycles(repo=repo, storage_path=store)
        for cycle in result["cycles"]:
            assert not any("z.py" in f for f in cycle)

    def test_result_shape(self, tmp_path):
        repo, store, _ = _build_linear_repo(tmp_path)
        result = get_dependency_cycles(repo=repo, storage_path=store)
        assert "repo" in result
        assert "cycle_count" in result
        assert "cycles" in result
        assert "_meta" in result
        assert "timing_ms" in result["_meta"]

    def test_unknown_repo_returns_error(self, tmp_path):
        store = str(tmp_path / "empty_store")
        result = get_dependency_cycles(repo="no_such_repo", storage_path=store)
        assert "error" in result

    def test_cycle_count_matches_cycles_list(self, tmp_path):
        repo, store = _build_cyclic_repo(tmp_path)
        result = get_dependency_cycles(repo=repo, storage_path=store)
        assert result["cycle_count"] == len(result["cycles"])


# ---------------------------------------------------------------------------
# get_coupling_metrics integration tests
# ---------------------------------------------------------------------------

class TestGetCouplingMetrics:

    def _find_file(self, store_path, repo, name):
        """Return the full indexed path for a file ending in *name*."""
        from jcodemunch_mcp.storage import IndexStore
        owner, rname = repo.split("/", 1)
        idx = IndexStore(base_path=store_path).load_index(owner, rname)
        return next(f for f in idx.source_files if f.endswith(name))

    def test_result_shape(self, tmp_path):
        repo, store, _ = _build_linear_repo(tmp_path)
        path = self._find_file(store, repo, "b.py")
        result = get_coupling_metrics(repo=repo, module_path=path, storage_path=store)
        assert "error" not in result, result.get("error")
        for key in ("repo", "module", "ca", "ce", "instability", "assessment",
                    "importers", "dependencies", "_meta"):
            assert key in result

    def test_leaf_node_high_instability(self, tmp_path):
        """c.py imports nothing — Ce=0, Ca>0 → maximally stable (I=0)."""
        repo, store, _ = _build_linear_repo(tmp_path)
        path = self._find_file(store, repo, "c.py")
        result = get_coupling_metrics(repo=repo, module_path=path, storage_path=store)
        assert result["ce"] == 0
        assert result["ca"] >= 1
        assert result["instability"] == 0.0
        assert result["assessment"] == "stable"

    def test_top_node_ce_positive(self, tmp_path):
        """a.py imports b — Ce >= 1."""
        repo, store, _ = _build_linear_repo(tmp_path)
        path = self._find_file(store, repo, "a.py")
        result = get_coupling_metrics(repo=repo, module_path=path, storage_path=store)
        assert result["ce"] >= 1

    def test_importers_list_correct(self, tmp_path):
        """b.py is imported by a.py — importers should contain a.py."""
        repo, store, _ = _build_linear_repo(tmp_path)
        path = self._find_file(store, repo, "b.py")
        result = get_coupling_metrics(repo=repo, module_path=path, storage_path=store)
        assert any("a.py" in imp for imp in result["importers"])

    def test_dependencies_list_correct(self, tmp_path):
        """b.py imports c.py — dependencies should contain c.py."""
        repo, store, _ = _build_linear_repo(tmp_path)
        path = self._find_file(store, repo, "b.py")
        result = get_coupling_metrics(repo=repo, module_path=path, storage_path=store)
        assert any("c.py" in dep for dep in result["dependencies"])

    def test_instability_range(self, tmp_path):
        repo, store, _ = _build_linear_repo(tmp_path)
        path = self._find_file(store, repo, "b.py")
        result = get_coupling_metrics(repo=repo, module_path=path, storage_path=store)
        assert result["instability"] is None or 0.0 <= result["instability"] <= 1.0

    def test_unknown_file_returns_error(self, tmp_path):
        repo, store, _ = _build_linear_repo(tmp_path)
        result = get_coupling_metrics(repo=repo, module_path="no/such/file.py", storage_path=store)
        assert "error" in result

    def test_unknown_repo_returns_error(self, tmp_path):
        store = str(tmp_path / "empty")
        result = get_coupling_metrics(repo="ghost/repo", module_path="x.py", storage_path=store)
        assert "error" in result


# ---------------------------------------------------------------------------
# _file_to_layer unit tests
# ---------------------------------------------------------------------------

class TestFileToLayer:

    def _layers(self):
        return [
            {"name": "api", "paths": ["src/api"]},
            {"name": "svc", "paths": ["src/svc"]},
            {"name": "db",  "paths": ["src/db"]},
        ]

    def test_matches_prefix(self):
        assert _file_to_layer("src/api/routes.py", self._layers()) == "api"

    def test_matches_db_prefix(self):
        assert _file_to_layer("src/db/models.py", self._layers()) == "db"

    def test_unassigned_returns_none(self):
        assert _file_to_layer("src/other/utils.py", self._layers()) is None

    def test_exact_path_match(self):
        layers = [{"name": "root", "paths": ["src/main.py"]}]
        assert _file_to_layer("src/main.py", layers) == "root"

    def test_no_partial_prefix_match(self):
        # src/apiother should NOT match src/api layer
        assert _file_to_layer("src/apiother/x.py", self._layers()) is None

    def test_empty_layers(self):
        assert _file_to_layer("src/api/x.py", []) is None


# ---------------------------------------------------------------------------
# get_layer_violations integration tests
# ---------------------------------------------------------------------------

class TestGetLayerViolations:

    def _rules(self, src_root=None):
        """Layer rules using exact filenames (as stored in the index)."""
        return [
            {"name": "api", "paths": ["routes.py"],  "may_not_import": ["db"]},
            {"name": "svc", "paths": ["service.py"], "may_not_import": []},
            {"name": "db",  "paths": ["models.py"],  "may_not_import": []},
        ]

    def test_result_shape(self, tmp_path):
        repo, store, src = _build_layered_repo(tmp_path)
        result = get_layer_violations(repo=repo, rules=self._rules(), storage_path=store)
        assert "error" not in result, result.get("error")
        for key in ("repo", "layer_count", "violation_count", "violations", "_meta"):
            assert key in result

    def test_violation_detected(self, tmp_path):
        """api/routes.py imports db/models.py — should be a violation."""
        repo, store, src = _build_layered_repo(tmp_path)
        result = get_layer_violations(repo=repo, rules=self._rules(), storage_path=store)
        assert result["violation_count"] >= 1
        # At least one violation should involve api → db
        api_to_db = [
            v for v in result["violations"]
            if v["file_layer"] == "api" and v["target_layer"] == "db"
        ]
        assert len(api_to_db) >= 1, f"Expected api→db violation, got: {result['violations']}"

    def test_violation_fields(self, tmp_path):
        repo, store, src = _build_layered_repo(tmp_path)
        result = get_layer_violations(repo=repo, rules=self._rules(), storage_path=store)
        for v in result["violations"]:
            assert "file" in v
            assert "file_layer" in v
            assert "import_target" in v
            assert "target_layer" in v
            assert "rule_violated" in v

    def test_no_false_positives_for_allowed_imports(self, tmp_path):
        """svc has no may_not_import rules — its imports to db must not be flagged."""
        repo, store, src = _build_layered_repo(tmp_path)
        result = get_layer_violations(repo=repo, rules=self._rules(), storage_path=store)
        for v in result["violations"]:
            # Only api-layer violations should appear (api may_not_import db)
            assert v["file_layer"] == "api", f"Unexpected violating layer: {v}"

    def test_no_rules_returns_note(self, tmp_path):
        repo, store, src = _build_layered_repo(tmp_path)
        result = get_layer_violations(repo=repo, rules=[], storage_path=store)
        assert "error" not in result
        assert result["violation_count"] == 0
        assert "note" in result

    def test_layer_count_matches_rules(self, tmp_path):
        repo, store, src = _build_layered_repo(tmp_path)
        rules = self._rules(src)
        result = get_layer_violations(repo=repo, rules=rules, storage_path=store)
        assert result["layer_count"] == len(rules)

    def test_unknown_repo_returns_error(self, tmp_path):
        store = str(tmp_path / "empty")
        result = get_layer_violations(repo="ghost/repo", storage_path=store)
        assert "error" in result

    def test_rule_violated_format(self, tmp_path):
        repo, store, src = _build_layered_repo(tmp_path)
        result = get_layer_violations(repo=repo, rules=self._rules(), storage_path=store)
        for v in result["violations"]:
            assert "may_not_import" in v["rule_violated"]
