---
name: testing-infrastructure
description: >
  Testing structure for Guanine (CodeEdit). Test plans in .claude/tests/,
  test code in tests/. Uses pytest. No tests exist yet — this defines
  the conventions for when they are added.
---

# Testing Infrastructure — Organization & Workflow

## Overview

We use a decentralized testing structure. Test plans, notes, and strategies live in `.claude/tests/` as markdown. Actual test code lives in `tests/`. When a feature has been fully tested and stabilized, the plans are archived in `history/`.

## Directory Structure

```
Guanine(CodeEdit)/
├── .claude/
│   └── tests/                           <- Active test plans and notes (.md)
│       ├── history/                     <- Completed/archived test plans
│       └── <feature>-test-plan.md       <- Active test plan for a specific feature
├── tests/
│   ├── conftest.py                      <- Shared fixtures
│   ├── test_scanner.py                  <- Tests for FileScanner
│   ├── test_merger.py                   <- Tests for MergeEngine
│   ├── test_extractor.py               <- Tests for EditorHistoryExtractor
│   ├── test_session.py                 <- Tests for SQLite session persistence
│   ├── test_routes.py                  <- Flask route tests (using test client)
│   └── fixtures/                        <- Sample entries.json, test directories
```

## Test Runner

- **Framework**: pytest
- **Run all tests**: `pytest tests/ -v`
- **Run a single module**: `pytest tests/test_scanner.py -v`
- **Dependencies**: `pip install pytest` (add `flask` test client for route tests)

## Workflow

### 1. Planning a Test

When requested to create a test plan for a new feature or bug fix:
- Create a markdown file in `.claude/tests/<feature-name>-test-plan.md`.
- Reference which design invariants (from `.claude/rules/invariants.md`) the tests validate.

### 2. Writing Tests

- Place all test code in `tests/`.
- Name test files `test_<component>` to mirror the class/component they test.
- **Emoji Restriction:** Never use emojis in test scripts, assertions, or printed output.

### 3. Archiving

Once a feature is considered fully tested, stable, and merged:
- Move the markdown test plan from `.claude/tests/` to `.claude/tests/history/`.

---

## What to Mock vs What to Test Real

### Never mock (use real instances):
- SQLite databases (use in-memory `:memory:` or temp file DBs)
- FileScanner logic (hashing, categorization, binary detection)
- MergeEngine diff generation
- Data model operations (FileVersion, MergeItem)

### Mock these:
- Filesystem operations for large directory trees (use `tmp_path` fixture)
- Editor history directories (create synthetic `entries.json` files in temp dirs)
- Flask SSE endpoints (test the underlying functions, not the streaming)
- `shutil.copy2` in merge execution tests (to avoid actual file writes in unit tests)

---

## Invariant Compliance Tests

These tests verify the design invariants from `.claude/rules/invariants.md`.
They are the highest-priority tests in the project.

| Invariant | What to test |
|-----------|-------------|
| #1 No Silent File Destruction | `MergeEngine.execute_merge()` skips files where target hash matches source hash; no file is written without a resolved selection |
| #2 Session Persistence | After `save_item_resolution()`, reloading via `_load_inventory_from_db()` returns the updated state; app restart via `_restore_session()` recovers all data |

---

## Test Fixtures

Shared test fixtures (`conftest.py`) should provide:
- Temporary source directories with known file contents and hashes
- Synthetic editor history directories with `entries.json` files
- In-memory or temp-file SQLite sessions
- Flask test client with `app.test_client()`
- Sample `MergeItem` and `FileVersion` objects for unit tests
