"""Shared pytest fixtures for jcodemunch-mcp tests."""

import pytest


@pytest.fixture(autouse=True)
def _clear_index_cache():
    """Clear the in-memory SQLite index cache before and after each test.

    Tests that modify the SQLite DB directly (e.g. changing index_version)
    can leave stale entries in the module-level cache.  SQLite WAL mode does
    not always update the main DB file mtime on write, so the cache key
    (owner, name, mtime_ns) may still match after a direct DB modification.
    Clearing before each test ensures no cross-test contamination; clearing
    after ensures no stale entries persist for the next test.
    """
    try:
        from jcodemunch_mcp.storage.sqlite_store import _cache_clear
        _cache_clear()
    except ImportError:
        pass
    yield
    try:
        from jcodemunch_mcp.storage.sqlite_store import _cache_clear
        _cache_clear()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _reset_global_config():
    """Reset the global config state between tests.

    Tests that call main() or load_config() modify _GLOBAL_CONFIG and
    _PROJECT_CONFIGS at module level.  Without cleanup, subsequent tests
    see config values left by earlier tests (e.g. 'watch': true from the
    user's disk config bleeds into tests that expect the default).
    """
    yield
    try:
        from jcodemunch_mcp import config as cfg
        from copy import deepcopy
        cfg._GLOBAL_CONFIG = deepcopy(cfg.DEFAULTS)
        cfg._PROJECT_CONFIGS.clear()
        cfg._PROJECT_CONFIG_HASHES.clear()
        cfg._REPO_PATH_CACHE.clear()
    except ImportError:
        pass
