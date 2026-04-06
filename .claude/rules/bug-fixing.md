---
name: bug-fixing
description: >
  Mandatory rules for any bug fix: root-cause analysis, invariant
  checks, defensive-skip prohibition, and pre-commit checklist.
  Read before starting any bug fix work.
---

# Bug Fixing Rules

## Before Fixing

1. **Root-cause analysis first.** Understand WHY the bug exists before writing a fix.
   Read the relevant module code and trace the data flow.

2. **Check invariants.** Does the bug violate a design invariant from `invariants.md`?
   If so, the fix must restore the invariant — not work around it.

3. **Read relevant doc rules.** Check if a documentation rule exists for the affected
   module (in `<module>/.claude/rules/docs/`). It will tell you about dependencies
   and impact areas.

4. **Check the spec / design doc.** If one exists, confirm the expected behavior
   before assuming what "correct" looks like.

## The Fix

- Fix the root cause, not the symptom
- Do NOT add defensive `try/except` / `try/catch` blocks to suppress errors
- Do NOT skip failing operations — understand why they fail
- Do NOT add "guard clauses" that silently return early to avoid the bug path
- Keep the fix minimal — do not refactor surrounding code
- If the fix touches a data flow (write, read, delete), trace the full flow
  to ensure the fix doesn't break a downstream step

## Prohibited Patterns

```python
# BAD — suppressing the error instead of fixing it
try:
    risky_operation()
except Exception:
    pass  # "just in case"

# BAD — skipping instead of fixing
if not data:
    return  # "defensive" early return that hides the real bug

# GOOD — fix the root cause
data = fetch_data()  # Fixed: was passing wrong ID to fetch
process(data)
```

## Pre-Commit Checklist

Before committing a bug fix, verify:

- [ ] The fix addresses the root cause, not a symptom
- [ ] No design invariant is violated (check `.claude/rules/invariants.md`)
- [ ] No silent error suppression was added
- [ ] The data flow through affected modules is intact
- [ ] Related modules are not broken by the change (check "Used by" in doc rules)
- [ ] A test can reproduce the bug and verify the fix
