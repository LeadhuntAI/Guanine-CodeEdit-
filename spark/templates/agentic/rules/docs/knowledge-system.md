---
name: knowledge-system
description: >
  How rules, knowledge sets, and skills are discovered, resolved, and injected
  into agent system prompts. Covers YAML frontmatter, resolve_knowledge, index
  builders, and on-demand discovery tools.
---

# Knowledge System — Rules, Skills & Prompt Injection

This doc explains how agents receive curated knowledge in their system prompts and how they can discover additional knowledge on demand.

> **Do NOT read the source code.** This documentation is self-contained. You do not need to open the engine Python files to understand or configure knowledge injection. Follow the structures and examples below. If something is unclear, ask the user.

---

## 1. Three Knowledge Categories

Agentic loop layers accept three knowledge fields:

| Field | What It Does | System Prompt Output |
|-------|-------------|---------------------|
| `rules` | List of file paths. Full content injected statically. | Raw markdown content |
| `knowledge_set` | List of file paths. Only summaries shown. | `<AVAILABLE_RULES>` index block |
| `skills` | List of skill names. Only summaries shown. | `<AVAILABLE_SKILLS>` index block |

**Design principle:** `rules` are always-loaded constraints (small files). `knowledge_set` and `skills` use progressive disclosure — summaries first, full content on demand via tools.

### Example Layer Definition

```json
{
  "layer_type": "agentic_loop",
  "knowledge": {
    "rules": ["rules/coding-standards.md"],
    "knowledge_set": [
      "rules/docs/workflows.md",
      "rules/docs/architecture.md"
    ],
    "skills": ["example-skill"],
    "base_dir": "."
  },
  "tools": ["read_file", "search_code"]
}
```

---

## 2. YAML Frontmatter Standard

All rule, doc, and skill markdown files use YAML frontmatter for metadata extraction.

### Format

```markdown
---
name: coding-standards
description: >
  Code quality standards and conventions. Covers naming, structure,
  error handling, and testing requirements.
---

# Coding Standards
...body content...
```

**Required fields:**
- **`name`** — Unique identifier. Used for matching and display.
- **`description`** — One-liner shown in index blocks. Drives skill routing decisions.

**Optional fields (skills only):**
- **`allowed-tools`** — List of tool names the skill expects.
- **`model`** — Preferred AI model (sub-agents).
- **`tools`** — Tool list (sub-agents).

### Fallback Parsing

If a file has no `---` frontmatter block, `extract_frontmatter()` falls back to extracting the first `#` heading as `name` and the first non-empty paragraph as `description`.

---

## 3. Resolution Pipeline

`resolve_knowledge()` in `engine/knowledge.py` is the central resolver. Called by the agentic loop processors.

### Input -> Output

```
rules=["path/to/rule.md"]
knowledge_set=["path/to/doc.md"]
skills=["example-skill"]
        |
        v
resolve_knowledge()
        |
        +-- rules_text:    Full content of rule files
        +-- rules_index:   <AVAILABLE_RULES> block from knowledge_set summaries
        +-- skills_index:  <AVAILABLE_SKILLS> block from discovered skills
```

### Processing Steps

1. **Rules** — Each path is resolved relative to `base_dir`, file content is read and concatenated.
2. **Knowledge set** — Each path is resolved. `build_rules_index()` calls `extract_frontmatter()` on each file to get its description, then builds the XML-tagged block.
3. **Skills** — `discover_skills()` scans `skills/` and `skill_definitions/` under `base_dir`. If specific skill names are provided, only those are included. `build_skills_index()` builds the block.

---

## 4. System Message Injection

The resolved knowledge is injected into the agent's system message in this order:

```
1. Base system message (from layer definition)
2. rules_text       — full rule content (if any)
3. rules_index      — <AVAILABLE_RULES> block (if any)
4. skills_index     — <AVAILABLE_SKILLS> block (if any)
5. Tools section    — tool descriptions (ReAct) or native schema
6. Format rules     — ReAct format instructions (ReAct mode only)
```

This applies to both `process_agentic_loop` (ReAct) and `process_agentic_loop_native` (function-calling).

---

## 5. Index Block Format

### `<AVAILABLE_RULES>`

```xml
<AVAILABLE_RULES>
- coding-standards: Code quality standards and conventions...
- architecture: System architecture overview and key decisions...
</AVAILABLE_RULES>
```

Use a tool like `read_file` to load a rule's full content when needed.

### `<AVAILABLE_SKILLS>`

```xml
<AVAILABLE_SKILLS>
- example-skill: Analyzes a codebase directory and produces a summary...
</AVAILABLE_SKILLS>
```

Use the `use_skill` tool to load a skill's full instructions.

---

## 6. On-Demand Discovery

Agents can discover and load knowledge at runtime using tools:

- **`read_file`** — Read any file's full content by path. Use this to load rules or reference documents on demand.
- **`search_code`** — Search for patterns in files. Use this to discover relevant rules or configuration.

You can also create custom tools like `search_rules` or `use_skill` that wrap the knowledge resolution functions. See `json-definitions.md` for tool creation details.

---

## 7. Helper Functions

| Function | Location | Purpose |
|----------|----------|---------|
| `extract_frontmatter(file_path)` | `engine/knowledge.py` | Parse YAML frontmatter or fallback to heading+paragraph |
| `build_rules_index(file_paths)` | `engine/knowledge.py` | Build `<AVAILABLE_RULES>` block from file paths |
| `build_skills_index(metadata_list)` | `engine/knowledge.py` | Build `<AVAILABLE_SKILLS>` block from skill metadata |
| `discover_skills(base_dir)` | `engine/knowledge.py` | Scan skill directories, return metadata list |
| `resolve_knowledge(rules, skills, knowledge_set, base_dir)` | `engine/knowledge.py` | Central resolver combining all three categories |
