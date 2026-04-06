"""Tests for JSON→SQLite migration."""

import json
from pathlib import Path

from jcodemunch_mcp.storage.index_store import IndexStore
from jcodemunch_mcp.storage.sqlite_store import SQLiteIndexStore


def test_migrate_json_to_sqlite(tmp_path):
    """JSON→SQLite migration preserves all index data."""
    # Manually write a legacy JSON index file (simulating old-format data)
    json_data = {
        "repo": "local/test-abc123",
        "owner": "local",
        "name": "test-abc123",
        "indexed_at": "2025-01-01T00:00:00",
        "index_version": 4,
        "source_files": ["main.py"],
        "languages": {"python": 1},
        "symbols": [{
            "id": "main.py::greet#function",
            "file": "main.py",
            "name": "greet",
            "qualified_name": "greet",
            "kind": "function",
            "language": "python",
            "signature": "def greet()",
            "summary": "",
            "docstring": "",
            "decorators": [],
            "keywords": [],
            "parent": None,
            "line": 1,
            "end_line": 3,
            "byte_offset": 0,
            "byte_length": 20,
            "content_hash": "",
            "ecosystem_context": "",
        }],
        "file_hashes": {"main.py": "hash1"},
        "git_head": "abc",
        "file_summaries": {"main.py": "Greeting module"},
        "source_root": "/tmp/proj",
        "display_name": "test",
        "file_languages": {"main.py": "python"},
        "imports": {"main.py": [{"specifier": "os", "names": ["path"]}]},
        "context_metadata": {},
        "file_blob_shas": {},
        "file_mtimes": {"main.py": 1234567890000000000.0},
    }

    json_path = tmp_path / "local-test-abc123.json"
    json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")

    # Migrate JSON → SQLite
    sqlite_store = SQLiteIndexStore(base_path=str(tmp_path))
    migrated = sqlite_store.migrate_from_json(json_path, "local", "test-abc123")

    assert migrated is not None, "migrate_from_json returned None"
    assert migrated.repo == "local/test-abc123"
    assert migrated.git_head == "abc"
    assert migrated.display_name == "test"
    assert migrated.file_hashes == {"main.py": "hash1"}
    assert migrated.file_mtimes == {"main.py": 1234567890000000000.0}
    assert migrated.imports == {"main.py": [{"specifier": "os", "names": ["path"]}]}
    assert len(migrated.symbols) == 1
    assert migrated.symbols[0]["name"] == "greet"

    # Verify .json.migrated exists and original .json is gone
    assert (tmp_path / "local-test-abc123.json.migrated").exists()
    assert not json_path.exists()
