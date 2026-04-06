---
name: example-skill
description: >
  Example skill that demonstrates the SKILL.md format. This skill analyzes
  a codebase directory and produces a summary of its structure and purpose.
  Use when asked to "analyze a directory" or "summarize a project".
allowed-tools:
  - read_file
  - list_directory
  - get_file_tree
---

# Example Skill: Directory Analyzer

This skill demonstrates the folder-based skill format. Replace this with your own multi-step instructions.

## Phase 1: Discover Structure

1. Use `get_file_tree` to get an overview of the directory.
2. Identify the main entry points (e.g., `main.py`, `index.js`, `app.py`).
3. Note the directory structure and any configuration files.

## Phase 2: Analyze Key Files

1. Use `read_file` to read the main entry points identified above.
2. Look for import statements to understand dependencies.
3. Identify the core abstractions (classes, functions, modules).

## Phase 3: Produce Summary

Combine your findings into a structured summary:

- **Purpose:** What does this project/directory do?
- **Structure:** How is the code organized?
- **Key files:** Which files are most important and why?
- **Dependencies:** What external libraries/modules are used?
- **Entry points:** How do you run or use this code?

Format the output as clean markdown.
