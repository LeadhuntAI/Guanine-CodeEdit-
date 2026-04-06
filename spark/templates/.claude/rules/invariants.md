---
name: design-invariants
description: >
  Non-negotiable architectural rules for the project. Violating any of
  these is a bug, regardless of whether tests pass. Read before any
  code change. Customise this file for your project.
---

# Design Invariants

These are non-negotiable architectural rules for this project.
Violating any of these is a bug, regardless of whether tests pass.

<!-- INSTRUCTIONS: Replace these examples with your project's real invariants.
     Good invariants are rules that:
     - If broken, cause data corruption, security issues, or silent failures
     - Apply everywhere, not just one module
     - Can be verified mechanically (a test can check them)
     
     For each invariant, list:
     - The rule itself (one sentence)
     - Why it exists (what breaks if violated)
     - Which files enforce it (so agents know where to look)
-->

## 1. [Example: Data Integrity Rule]

[Description: e.g., "Database write MUST complete before cache update."]

- **Why**: [What breaks — e.g., "Prevents stale cache serving data that was never persisted"]
- **Files**: [Which modules enforce this — e.g., `services/data.py`, `cache/manager.py`]

## 2. [Example: Security / Isolation Rule]

[Description: e.g., "Every database query MUST filter by tenant_id."]

- **Why**: [What breaks — e.g., "Missing filter = data breach across tenants"]
- **Files**: [e.g., `db/repository.py`, `middleware/tenant.py`]

## 3. [Example: Consistency Rule]

[Description: e.g., "All API responses MUST include a request_id for tracing."]

- **Why**: [What breaks — e.g., "Without request_id, production debugging is impossible"]
- **Files**: [e.g., `middleware/request_id.py`, `api/base.py`]

<!--
More invariant ideas to consider for your project:

DATA INTEGRITY:
- Write-ahead / write-behind ordering
- Foreign key / referential integrity across services
- Idempotency requirements on writes
- Eventual consistency guarantees and their boundaries

SECURITY:
- Tenant / user isolation at the query layer
- Input validation at system boundaries
- Auth check enforcement (where, not just if)
- Secrets never in logs or responses

CONSISTENCY:
- Schema versioning rules
- API contract guarantees
- State machine transitions (valid state → valid state only)
- Ordering guarantees (events, messages, operations)

PERFORMANCE:
- N+1 query prevention
- Pagination requirements on list endpoints
- Timeout/circuit-breaker requirements on external calls
-->
