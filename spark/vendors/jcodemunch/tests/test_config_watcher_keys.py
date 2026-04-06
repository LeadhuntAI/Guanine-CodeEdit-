"""Tests for watcher config keys in config.py."""
from jcodemunch_mcp import config as config_module


class TestWatcherConfigDefaults:
    """New watcher config keys have correct defaults and types."""

    def test_watch_paths_default_is_empty_list(self):
        assert config_module.DEFAULTS["watch_paths"] == []

    def test_watch_idle_timeout_default_is_none(self):
        assert config_module.DEFAULTS["watch_idle_timeout"] is None

    def test_watch_extra_ignore_default_is_empty_list(self):
        assert config_module.DEFAULTS["watch_extra_ignore"] == []

    def test_watch_follow_symlinks_default_is_false(self):
        assert config_module.DEFAULTS["watch_follow_symlinks"] is False

    def test_watch_log_default_is_none(self):
        assert config_module.DEFAULTS["watch_log"] is None

    def test_watch_paths_type_is_list(self):
        assert config_module.CONFIG_TYPES["watch_paths"] is list

    def test_watch_idle_timeout_type_allows_int_or_none(self):
        assert config_module.CONFIG_TYPES["watch_idle_timeout"] == (int, type(None))

    def test_watch_extra_ignore_type_is_list(self):
        assert config_module.CONFIG_TYPES["watch_extra_ignore"] is list

    def test_watch_follow_symlinks_type_is_bool(self):
        assert config_module.CONFIG_TYPES["watch_follow_symlinks"] is bool

    def test_watch_log_type_allows_str_or_none(self):
        assert config_module.CONFIG_TYPES["watch_log"] == (str, type(None))
