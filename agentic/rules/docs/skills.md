---
name: skills
description: >
  Skills architecture for the agentic system. Covers folder structure,
  SKILL.md format, JSON definitions, discovery, and sub-agents.
---

# Skills — Architecture & Creation Guide

Skills are multi-step procedural definitions that teach agents how to accomplish complex tasks. They provide structured instructions that agents load on demand during workflow execution.

> **Do NOT read the source code.** This documentation is self-contained. You do not need to open the engine Python files to create or integrate skills. Follow the structures and examples below. If something is unclear, ask the user.

---

## 1. Overview

- **Location:** `agentic/skills/<skill-name>/SKILL.md` or `agentic/skill_definitions/<name>.json`
- **Used by:** Agents running inside `agentic_loop` layers during workflow execution
- **Discovery:** `discover_skills()` in `engine/knowledge.py` scans both locations
- **Access:** Agents load skills on demand when they decide they need the instructions

Skills are discovered automatically. Place them in the correct directory and they will appear in the `<AVAILABLE_SKILLS>` index shown to agents.

---

## 2. Folder-Based Skills (`SKILL.md`)

### Directory Structure

```
agentic/skills/<skill-name>/
├── SKILL.md           # Required — skill definition with YAML frontmatter
├── references/        # Optional — template files, examples, reference data
├── agents/            # Optional — sub-agent definitions (*.md with frontmatter)
├── scripts/           # Optional — helper scripts the agent can execute
└── assets/            # Optional — images, data files
```

### SKILL.md Format

```markdown
---
name: code-reviewer
description: >
  Reviews code changes for quality, security, and best practices.
  Produces a structured review with findings and recommendations.
  Use when the user asks to "review code" or "check this PR".
allowed-tools:
  - read_file
  - search_code
  - list_directory
---

# Code Reviewer

## Phase 1: Understand Context
1. Use `list_directory` to see the project structure...

## Phase 2: Analyze Changes
1. Use `read_file` to examine each changed file...

## Phase 3: Produce Review
Combine findings into a structured review...
```

**YAML frontmatter fields:**
- **`name`** (required) — Unique skill identifier, used for matching
- **`description`** (required) — Used for routing. The LLM reads this to decide whether to invoke the skill. Write it to clearly describe when and why to use the skill.
- **`allowed-tools`** (optional) — List of tool names this skill expects.

**Body:** Free-form markdown with phased instructions. The body is what gets returned to the agent when it loads the skill.

---

## 3. JSON Skill Definitions

Alternative to folder-based skills. Place `.json` files in `agentic/skill_definitions/`.

```json
{
  "name": "data-cleaner",
  "description": "Cleans and normalises CSV data files.",
  "allowed_tools": ["read_file", "write_file"],
  "phases": [
    { "name": "Load", "instructions": "Read the CSV file and examine its structure." },
    { "name": "Clean", "instructions": "Fix formatting issues, remove duplicates, normalise values." }
  ]
}
```

JSON skills are simpler (no sub-agents, no references folder) but work with the same discovery mechanism.

---

## 4. Skill Discovery

`discover_skills(base_dir)` in `engine/knowledge.py` scans:

1. `skills/*/SKILL.md` — folder-based skills
2. `skill_definitions/*.json` — JSON skills

Returns a list of metadata dicts: `[{name, description, path, type}, ...]`

Called by `resolve_knowledge()` when building the `<AVAILABLE_SKILLS>` index for agent system prompts.

---

## 5. Using Skills at Runtime

### Progressive Disclosure

Skills use a two-stage loading pattern to minimise token usage:

1. **Index stage** — Agent sees `<AVAILABLE_SKILLS>` block in its system message with skill names and descriptions only.
2. **Load stage** — Agent loads the full skill instructions when it decides it needs them.

This means agents only consume tokens for the full skill body when they actually need it.

### Workflow Integration

To make skills available to an agentic loop, add them to the layer's knowledge configuration:

```json
{
  "layer_type": "agentic_loop",
  "knowledge": {
    "skills": ["code-reviewer", "data-cleaner"],
    "base_dir": "."
  },
  "tools": ["read_file", "search_code"]
}
```

The `skills` field controls which skills appear in the `<AVAILABLE_SKILLS>` index.

---

## 6. Sub-Agents

Skills can define sub-agents in their `agents/` folder. Each sub-agent is a markdown file with YAML frontmatter specifying its model, tools, and instructions.

### Sub-Agent Definition (`agents/validator.md`)

```markdown
---
name: validator
description: Validates the review findings for accuracy.
model: openai/gpt-4o-mini
tools:
  - read_file
---

# Validation Agent

Check each finding in the review...
```

### Discovery and Execution

Sub-agents are discovered by scanning `agents/*.md` and extracting frontmatter. The parent agent can delegate subtasks to sub-agents, which run as independent lightweight agentic loops and return their output.

---

## 7. Creating a New Skill

Step-by-step:

1. Create the folder: `agentic/skills/<skill-name>/`
2. Create `SKILL.md` with YAML frontmatter (`name`, `description`, `allowed-tools`)
3. Write phased instructions in the body
4. (Optional) Add reference files to `references/`
5. (Optional) Add sub-agent definitions to `agents/`
6. Add `"skills": ["<skill-name>"]` to your agentic loop layer's knowledge configuration
7. The skill will be discovered automatically by `discover_skills()`
