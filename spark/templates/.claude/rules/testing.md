---
name: testing-infrastructure
description: >
  Decentralized testing structure: test plans in .claude/tests/,
  test scripts in the project's test directory, archive completed
  plans to history/. Covers mock policy and invariant compliance.
  Customise this file for your project's stack and test runner.
---

# Testing Infrastructure — Organization & Workflow

## Overview

We use a decentralized testing structure. Test plans, notes, and strategies live in `.claude/tests/` as markdown. Actual test code lives in your project's test directory. When a feature has been fully tested and stabilized, the plans are archived in `history/`.

## Directory Structure

<!-- Adapt this to your project's test directory structure. -->

```
project-root/
├── .claude/
│   └── tests/                           <- Active test plans and notes (.md)
│       ├── history/                     <- Completed/archived test plans
│       └── <feature>-test-plan.md       <- Active test plan for a specific feature
├── [your-source]/
│   └── tests/
│       ├── conftest.py                  <- Shared fixtures (if pytest)
│       ├── test_<module>.py             <- Test file per source module
│       ├── test_flows.py                <- End-to-end flow tests
│       └── fixtures/                    <- Test data, mocks, sample inputs
```

## Workflow

### 1. Planning a Test

When requested to create a test plan for a new feature or bug fix:
- Create a markdown file in `.claude/tests/<feature-name>-test-plan.md`.
- Use a clear structure: Overview, Test Environment, Tier 1 (Unit), Tier 2 (Integration), Tier 3 (End-to-End), Edge Cases.
- Include specific code snippets or shell commands to verify behavior.
- Reference which design invariants (from `.claude/rules/invariants.md`) the tests validate.

### 2. Writing Tests

<!-- Customise for your test framework (pytest, jest, go test, etc.) -->

- Place all test code in `[your test directory]`.
- Name test files `test_<module>` to mirror the source module they test.
- Flow-level tests that span multiple modules go in `test_flows`.
- **Emoji Restriction:** Never use emojis in test scripts, assertions, or printed output.

### 3. Execution & Validation

<!-- Customise your test command. -->

- Run all tests: `[your test command, e.g., pytest tests/ -v]`
- Run a single module: `[e.g., pytest tests/test_router.py -v]`
- Follow the steps in the `.md` plan and check off items as they pass.

### 4. Archiving

Once a feature is considered fully tested, stable, and merged:
- Move the markdown test plan from `.claude/tests/` to `.claude/tests/history/`.
- This keeps the active plans directory clean and focused on current work.

---

## What to Mock vs What to Test Real

<!-- Customise this for your project. The principle: mock at system
     boundaries (external APIs, queues, third-party services), test
     real at internal boundaries (your own database, your own logic). -->

### Never mock (use real instances):
- Your database (use in-memory or test instances)
- Your own business logic and pure functions
- Internal module-to-module calls

### Mock these:
- External API calls (third-party services, AI providers)
- Message queues and background workers (test the function directly)
- File system / network operations that are slow or non-deterministic
- Time-dependent operations (use a fixed clock)

---

## Invariant Compliance Tests

<!-- This is the most important test category. For each invariant in
     .claude/rules/invariants.md, define what specific test assertions
     verify that the invariant holds. -->

These tests verify the design invariants from `.claude/rules/invariants.md`.
They are the highest-priority tests in the project.

| Invariant | What to test |
|-----------|-------------|
| #1 [Name] | [Specific assertion — e.g., "Record exists in DB before cache is populated"] |
| #2 [Name] | [Specific assertion] |
| #3 [Name] | [Specific assertion] |

---

## Test Fixtures

<!-- Describe the shared fixtures your conftest.py (or equivalent) should provide. -->

Shared test fixtures should provide:
- Isolated database sessions (no cross-test contamination)
- Deterministic test data
- Mocked external services with predictable responses
- Configuration overrides for test environments
