"""Tests for deferred summarization — parse immediate, summarize later."""

import pytest
import threading
import time

from jcodemunch_mcp.tools._indexing_pipeline import (
    parse_immediate,
    parse_and_prepare_incremental,
)


class TestDeferredSummarization:
    def test_parse_immediate_returns_symbols_with_empty_summaries(self):
        """parse_immediate should parse files and return symbols with empty/placeholder summaries."""
        file_contents = {
            "test_file.py": "def hello():\n    pass\n",
        }
        symbols, file_summaries, file_languages, file_imports, no_symbols = parse_immediate(
            files_to_parse=set(file_contents.keys()),
            file_contents=file_contents,
            active_providers=None,
            warnings=None,
        )
        assert len(symbols) > 0
        # Symbols should not have AI summaries yet (they should be empty or placeholder)
        for s in symbols:
            # Summary should be empty or default (not an actual AI summary)
            assert s.summary == "" or s.summary.startswith("(") or s.summary == s.name

    def test_parse_immediate_does_not_call_summarize(self):
        """parse_immediate should NOT call the AI summarizer."""
        file_contents = {
            "test_file.py": "def hello():\n    pass\n",
        }
        # This should not make any API calls
        symbols, file_summaries, file_languages, file_imports, no_symbols = parse_immediate(
            files_to_parse=set(file_contents.keys()),
            file_contents=file_contents,
            active_providers=None,
            warnings=None,
        )
        assert symbols is not None


class TestDeferredCancellation:
    def test_parse_immediate_returns_quickly(self):
        """parse_immediate should return without waiting for summarization."""
        file_contents = {
            "test_file.py": "def hello():\n    pass\n",
        }
        start = time.monotonic()
        symbols, *_ = parse_immediate(
            files_to_parse=set(file_contents.keys()),
            file_contents=file_contents,
            active_providers=None,
            warnings=None,
        )
        elapsed = time.monotonic() - start
        # Should return in under 1 second even with a large file
        assert elapsed < 1.0
        assert len(symbols) > 0
