"""Tests for CSS symbol extraction."""

import pytest

from src.jcodemunch_mcp.parser.extractor import parse_file, _parse_css_symbols
from src.jcodemunch_mcp.parser.languages import get_language_for_path, LANGUAGE_EXTENSIONS


# ---------------------------------------------------------------------------
# Extension / language detection
# ---------------------------------------------------------------------------

def test_css_extension_detected():
    assert get_language_for_path("static/css/main.css") == "css"


def test_css_extension_in_registry():
    assert ".css" in LANGUAGE_EXTENSIONS
    assert LANGUAGE_EXTENSIONS[".css"] == "css"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CSS_SRC = b"""
:root {
  --primary-color: #333;
  --spacing: 8px;
}

body {
  margin: 0;
  padding: 0;
  font-family: sans-serif;
}

.container {
  display: flex;
  max-width: 1200px;
}

.navbar .item {
  color: red;
}

#header {
  font-size: 24px;
  font-weight: bold;
}

h1, h2, h3 {
  font-weight: bold;
}

@keyframes slideIn {
  from { transform: translateX(-100%); }
  to   { transform: translateX(0); }
}

@media (max-width: 768px) {
  .container { flex-direction: column; }
}

@supports (display: grid) {
  .layout { display: grid; }
}
"""


def _syms():
    return _parse_css_symbols(_CSS_SRC, "styles/main.css")


def _parse_syms():
    return parse_file(_CSS_SRC.decode(), "styles/main.css", "css")


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------

def test_css_returns_symbols():
    assert len(_syms()) >= 7


def test_css_rule_sets_extracted():
    syms = _syms()
    names = {s.name for s in syms}
    assert ":root" in names
    assert "body" in names
    assert ".container" in names


def test_css_class_selector():
    syms = _syms()
    s = next(s for s in syms if s.name == ".container")
    assert s.kind == "class"
    assert s.language == "css"
    assert s.line > 0


def test_css_id_selector():
    syms = _syms()
    s = next(s for s in syms if s.name == "#header")
    assert s.kind == "class"


def test_css_tag_selector():
    syms = _syms()
    s = next(s for s in syms if s.name == "body")
    assert s.kind == "class"


def test_css_compound_selector():
    syms = _syms()
    s = next(s for s in syms if s.name == ".navbar .item")
    assert s.kind == "class"


def test_css_grouped_selectors():
    syms = _syms()
    s = next(s for s in syms if s.name == "h1, h2, h3")
    assert s.kind == "class"


def test_css_keyframes_extracted():
    syms = _syms()
    kf = next(s for s in syms if "@keyframes" in s.name)
    assert kf.name == "@keyframes slideIn"
    assert kf.kind == "function"


def test_css_media_query_extracted():
    syms = _syms()
    mq = next(s for s in syms if "@media" in s.name)
    assert "@media" in mq.name
    assert mq.kind == "type"


def test_css_supports_extracted():
    syms = _syms()
    sup = next(s for s in syms if "@supports" in s.name)
    assert sup.kind == "type"


def test_css_symbol_ids_unique():
    syms = _syms()
    ids = [s.id for s in syms]
    assert len(ids) == len(set(ids)), "Duplicate symbol IDs found"


def test_css_symbol_has_byte_info():
    syms = _syms()
    for s in syms:
        assert s.byte_offset >= 0
        assert s.byte_length > 0
        assert s.content_hash != ""


def test_css_via_parse_file():
    """parse_file() should dispatch to _parse_css_symbols."""
    syms = _parse_syms()
    assert len(syms) >= 7
    names = {s.name for s in syms}
    assert ".container" in names
    assert "@keyframes slideIn" in names


def test_css_empty_file():
    assert _parse_css_symbols(b"", "empty.css") == []
    assert _parse_css_symbols(b"   \n\n   ", "blank.css") == []


def test_css_no_symbols_file():
    # File with only comments — no rule sets
    src = b"/* just a comment */\n/* another */\n"
    assert _parse_css_symbols(src, "comments.css") == []
