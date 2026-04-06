# CLAUDE.md — [PROJECT_NAME]

## Project Overview

**[PROJECT_NAME]** — [2-3 sentence description of what this project does and why it exists].

Stack: [list your tech stack — e.g., Python, FastAPI, PostgreSQL, Redis, Docker]

## Rule and Skill Discovery Protocol

Do NOT expect a full list of rules or skills in this file.
The project uses a decentralised documentation system. Follow this protocol:

### Before starting any task:

1. **Identify affected modules.** Look at the file paths involved in the task.
   Map each path to its module or app.

2. **Read the module's index.** For each affected module, read:
   ```
   <module-path>/.claude/RULES_INDEX.md
   ```
   This lists all documentation rules, coding rules, and skills
   available for that module with one-line summaries.

3. **Load relevant rules only.** Based on the index and the specific
   files you are editing, read only the rules that apply.
   Do NOT load rules for unrelated modules or files.

4. **Check global rules.** Always check these locations for project-wide
   rules that apply regardless of module:
   ```
   .claude/RULES_INDEX.md           (global rules index)
   .claude/rules/                   (global coding rules)
   .claude/rules/invariants.md      (design invariants — MUST NOT be violated)
   ```

### For cross-module tasks:

If a task spans multiple modules, read the RULES_INDEX.md for ALL affected
modules and load rules from each that are relevant.

### After completing code changes:

If you created or significantly modified a file, check if a documentation
rule exists for it. If one exists and may be stale, ask: "Should I update
the documentation rule for this file?" If the user agrees, use the
`code-documenter` skill.

## Architecture

<!-- Replace with your project's architecture. List each major component
     with its file/module and a one-line description. -->

- **[Component A]** (`path/to/module.py`) — [what it does]
- **[Component B]** (`path/to/other.py`) — [what it does]
- **[Component C]** (`path/to/service.py`) — [what it does]

## Directory Layout

<!-- Replace with your actual directory layout. Show where .claude/
     directories live at each level. -->

```
[PROJECT_NAME]/
├── CLAUDE.md                              <- You are here
├── .claude/
│   ├── RULES_INDEX.md                     <- Global rules index
│   ├── rules/
│   │   ├── invariants.md                  <- Design invariants (NEVER violate)
│   │   ├── testing.md                     <- How tests are organized
│   │   ├── bug-fixing.md                  <- Bug fix discipline
│   │   └── docs/                          <- Cross-module documentation rules
│   ├── skills/
│   │   └── code-documenter/               <- Skill: analyse & document code
│   └── tests/
│       ├── <feature>-test-plan.md         <- Active test plans
│       └── history/                       <- Archived completed plans
├── [module-a]/
│   ├── .claude/
│   │   ├── RULES_INDEX.md                 <- Module-specific rules index
│   │   └── rules/docs/                   <- Module-specific documentation
│   └── ...source code...
└── [module-b]/
    ├── .claude/
    │   ├── RULES_INDEX.md
    │   └── rules/docs/
    └── ...source code...
```

## Key Conventions

- Documentation rules are stored in `.claude/rules/docs/` — either at
  the project root (cross-module) or inside a module's own `.claude/rules/docs/`.
- Coding convention rules are stored in `.claude/rules/` (no `docs/` subfolder).
- Skills are stored in `.claude/skills/<skill-name>/SKILL.md`.
- Module-specific rules override global rules when there is a conflict.

## Design Invariants (MUST NOT Violate)

<!-- Replace with your project's non-negotiable rules. These are the
     constraints that, if broken, constitute a bug regardless of whether
     tests pass. See .claude/rules/invariants.md for full details. -->

Read `.claude/rules/invariants.md` for the full list with file references.

1. **[Invariant 1]**: [description]
2. **[Invariant 2]**: [description]
3. **[Invariant 3]**: [description]

## Critical Data Flows

<!-- Document the major data flows through your system. For each flow,
     list which modules are involved and what must be true at each step. -->

### [Flow Name] (touches: `module_a` → `module_b` → `module_c`)

```
1. [Step 1]
2. [Step 2]
3. [Step 3]
```

## Testing Protocol

When writing or executing tests, **read `.claude/rules/testing.md` first**. Key points:

1. **Test plans** (markdown) go in `.claude/tests/<feature>-test-plan.md`.
   Archive completed plans to `.claude/tests/history/`.
2. **Test code** goes in `[your test directory]`.
3. [Project-specific testing conventions]
4. Run tests: `[your test command]`

## Configuration

<!-- List your configuration sources and key environment variables. -->

## General Coding Guidelines

<!-- List your project-specific coding conventions. Examples: -->

- [Language version and syntax preferences]
- [Import ordering]
- [Error handling policy]
- [Dependency management rules]
- Only modify code within the scope of the current request
- Always check for existing functions before creating new ones

## Bug Fixing Guidelines

When working on any bug fix, read `.claude/rules/bug-fixing.md` first.
Key points:
- Root-cause analysis before fixing
- Check if the bug violates a design invariant
- [Project-specific bug fix rules]

## Environment Notes

<!-- Platform-specific notes for your development environment. -->

- Developed on: [OS]
- Shell syntax: [bash/powershell/etc.]
- Package manager: [pip/npm/cargo/etc.]
