---
name: design-invariants
description: >
  Non-negotiable architectural rules for Guanine (CodeEdit).
  Violating any of these is a bug, regardless of whether tests pass.
  Read before any code change.
---

# Design Invariants

These are non-negotiable architectural rules for this project.
Violating any of these is a bug, regardless of whether tests pass.

## 1. No Silent File Destruction

Never delete or overwrite user files without explicit confirmation. The merge engine must skip files that already exist at the target with identical content (hash comparison), and all overwrites require explicit user action (e.g., clicking "resolve", confirming merge).

- **Why**: This is a code review tool — silently destroying files would be catastrophic and defeat the entire purpose of the tool.
- **Files**: `file_merger.py` — `MergeEngine.execute_merge()` (checks `existing_hash == version.sha256` before skipping), Flask route `/execute` (requires POST), `/resolve` (requires explicit selection), `agent_review.py` — merge-back route requires explicit accept

## 2. Session Data Must Always Be Persisted to SQLite

Session data must always be persisted to SQLite, never held only in memory. Individual conflict resolutions must use `save_item_resolution()` for instant single-row updates. Full inventory saves happen after scans via `save_inventory_state()`. The global `state` dict is a runtime cache — any mutation that the user would expect to survive a restart must be written to SQLite.

- **Why**: The tool handles potentially hours-long scan and resolution sessions. If the app crashes or restarts, losing all conflict resolutions would force the user to redo significant manual work. SQLite with WAL mode ensures durability without blocking reads.
- **Files**: `file_merger.py` — `save_item_resolution()`, `save_batch_resolutions()`, `save_inventory_state()`, `save_config()`, `save_session_meta()`, `_auto_save_state()`, `_auto_save_full()`, `_restore_session()`
