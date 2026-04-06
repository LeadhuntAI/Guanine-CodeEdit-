"""Tests for adaptive languages feature (languages_adaptive config key)."""

import json
from pathlib import Path

import pytest

from jcodemunch_mcp.config import (
    _build_languages_block,
    _check_raw_local_adaptive,
    _parse_active_languages,
    apply_adaptive_languages,
    invalidate_project_config_cache,
)


# ─── _parse_active_languages ────────────────────────────────────────────────


class TestParseActiveLanguages:
    def test_normal_array_all_active(self):
        content = '{"languages": ["python", "javascript"]}'
        assert _parse_active_languages(content) == {"python", "javascript"}

    def test_with_inline_comments(self):
        content = '''{
  "languages": [
    "python",
    // "javascript",
    "rust"
  ]
}'''
        assert _parse_active_languages(content) == {"python", "rust"}

    def test_null_languages(self):
        content = '{"languages": null}'
        assert _parse_active_languages(content) is None

    def test_absent_languages_key(self):
        content = '{"other": true}'
        assert _parse_active_languages(content) is None

    def test_block_comment_on_same_line(self):
        content = '''{
  "languages": [
    "python", // "javascript",
    "rust"
  ]
}'''
        assert _parse_active_languages(content) == {"python", "rust"}


# ─── _build_languages_block ─────────────────────────────────────────────────


class TestBuildLanguagesBlock:
    def test_detected_subset(self):
        from jcodemunch_mcp.parser.languages import LANGUAGE_REGISTRY

        detected = {"python", "javascript"}
        block = _build_languages_block(detected)
        assert '"python",' in block
        assert '"javascript",' in block
        # Undetected languages should be commented
        for lang in LANGUAGE_REGISTRY:
            if lang not in detected:
                assert f'// "{lang}",' in block

    def test_empty_detected(self):
        block = _build_languages_block(set())
        # All should be commented (no uncommented "python" line)
        assert '// "' in block
        import re

        assert re.search(r'^\s+"python",', block, re.MULTILINE) is None


# ─── invalidate_project_config_cache ──────────────────────────────────────


class TestInvalidateProjectConfigCache:
    def test_evicts_from_cache(self, monkeypatch):
        # Prime the cache directly
        from jcodemunch_mcp import config as _cfg

        test_path = "/fake/project/root"
        resolved = str(Path(test_path).resolve())
        _cfg._PROJECT_CONFIGS[resolved] = {"languages": ["python"]}
        _cfg._PROJECT_CONFIG_HASHES[resolved] = "abc123"

        invalidate_project_config_cache(test_path)

        assert resolved not in _cfg._PROJECT_CONFIGS
        assert resolved not in _cfg._PROJECT_CONFIG_HASHES


# ─── _check_raw_local_adaptive ──────────────────────────────────────────────


class TestCheckRawLocalAdaptive:
    def test_true_in_local_config(self, tmp_path):
        cfg = tmp_path / ".jcodemunch.jsonc"
        cfg.write_text('{"languages_adaptive": true, "languages": ["python"]}', encoding="utf-8")
        is_adaptive, content = _check_raw_local_adaptive(cfg)
        assert is_adaptive is True
        assert content == '{"languages_adaptive": true, "languages": ["python"]}'

    def test_false_in_local_config(self, tmp_path):
        cfg = tmp_path / ".jcodemunch.jsonc"
        cfg.write_text('{"languages_adaptive": false}', encoding="utf-8")
        is_adaptive, content = _check_raw_local_adaptive(cfg)
        assert is_adaptive is False

    def test_absent_key(self, tmp_path):
        cfg = tmp_path / ".jcodemunch.jsonc"
        cfg.write_text('{"languages": ["python"]}', encoding="utf-8")
        is_adaptive, content = _check_raw_local_adaptive(cfg)
        assert is_adaptive is False

    def test_invalid_json(self, tmp_path):
        cfg = tmp_path / ".jcodemunch.jsonc"
        cfg.write_text('not valid json{', encoding="utf-8")
        is_adaptive, content = _check_raw_local_adaptive(cfg)
        assert is_adaptive is False
        assert content == ""


# ─── apply_adaptive_languages ────────────────────────────────────────────────


class TestApplyAdaptiveLanguages:
    def test_no_local_global_adaptive_creates_file(self, tmp_path, monkeypatch):
        # No local config exists; global adaptive is True
        monkeypatch.setattr("jcodemunch_mcp.config._GLOBAL_CONFIG", {"languages_adaptive": True})

        detected = {"python", "rust"}
        result = apply_adaptive_languages(str(tmp_path), detected)

        local_cfg = tmp_path / ".jcodemunch.jsonc"
        assert local_cfg.exists()
        # languages_adaptive should be present in created file
        from jcodemunch_mcp.config import _strip_jsonc

        content = local_cfg.read_text(encoding="utf-8")
        parsed = json.loads(_strip_jsonc(content))
        assert parsed.get("languages_adaptive") is True

    def test_local_exists_adaptive_true_updates_languages(self, tmp_path, monkeypatch):
        monkeypatch.setattr("jcodemunch_mcp.config._GLOBAL_CONFIG", {})

        local_cfg = tmp_path / ".jcodemunch.jsonc"
        local_cfg.write_text(
            '{"languages_adaptive": true, "languages": ["python"]}',
            encoding="utf-8",
        )

        detected = {"python", "javascript", "rust"}
        result = apply_adaptive_languages(str(tmp_path), detected)

        assert result is True
        content = local_cfg.read_text(encoding="utf-8")
        # New languages should appear
        for lang in detected:
            assert f'"{lang}"' in content

    def test_noop_when_adaptive_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr("jcodemunch_mcp.config._GLOBAL_CONFIG", {})

        local_cfg = tmp_path / ".jcodemunch.jsonc"
        local_cfg.write_text(
            '{"languages_adaptive": false, "languages": ["python"]}',
            encoding="utf-8",
        )
        original_content = local_cfg.read_text(encoding="utf-8")

        detected = {"python", "rust"}
        result = apply_adaptive_languages(str(tmp_path), detected)

        assert result is False
        assert local_cfg.read_text(encoding="utf-8") == original_content

    def test_noop_when_no_change_needed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("jcodemunch_mcp.config._GLOBAL_CONFIG", {"languages_adaptive": True})

        local_cfg = tmp_path / ".jcodemunch.jsonc"
        local_cfg.write_text(
            '{"languages_adaptive": true, "languages": ["python"]}',
            encoding="utf-8",
        )
        original_content = local_cfg.read_text(encoding="utf-8")

        # Same detection result
        detected = {"python"}
        result = apply_adaptive_languages(str(tmp_path), detected)

        assert result is False
        assert local_cfg.read_text(encoding="utf-8") == original_content


def _strip_for_test(content: str) -> str:
    """Strip JSONC comments for parsing in tests."""
    import re

    # Remove line comments
    content = re.sub(r"//.*$", "", content, flags=re.MULTILINE)
    # Remove block comments
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    return content
