"""Add or update an entry in a RULES_INDEX.md file."""

from __future__ import annotations

import json
import os
import re


def _validate_path(path: str, base_dir: str) -> str | None:
    resolved = os.path.realpath(os.path.join(base_dir, path))
    base = os.path.realpath(base_dir)
    if not resolved.startswith(base + os.sep) and resolved != base:
        return None
    return resolved


def execute(
    index_path: str,
    entry_path: str,
    summary: str,
    section: str = "Documentation Rules",
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """Add or update an entry in RULES_INDEX.md. Returns JSON string."""
    try:
        resolved = _validate_path(index_path, _base_dir)
        if resolved is None:
            return json.dumps({"error": "Path escapes base directory"})

        # Read existing content or create new
        if os.path.isfile(resolved):
            with open(resolved, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            content = "# Rules Index\n\nAuto-generated index of project rules and documentation.\n"

        new_entry = f"- [{entry_path}]({entry_path}) — {summary}"

        # Find the section
        section_heading = f"## {section}"
        section_pattern = re.compile(
            rf"^(## {re.escape(section)}\s*\n)",
            re.MULTILINE,
        )

        match = section_pattern.search(content)
        action = "added"

        if match:
            # Section exists — check if entry already present
            section_start = match.end()
            # Find end of section (next ## heading or end of file)
            next_heading = re.search(r"^## ", content[section_start:], re.MULTILINE)
            section_end = section_start + next_heading.start() if next_heading else len(content)
            section_text = content[section_start:section_end]

            # Check for existing entry by path
            entry_line_pattern = re.compile(
                rf"^- \[{re.escape(entry_path)}\].*$",
                re.MULTILINE,
            )
            entry_match = entry_line_pattern.search(section_text)

            if entry_match:
                # Update existing entry
                abs_start = section_start + entry_match.start()
                abs_end = section_start + entry_match.end()
                content = content[:abs_start] + new_entry + content[abs_end:]
                action = "updated"
            else:
                # Add new entry at end of section (before any trailing blank lines and next heading)
                insert_pos = section_end
                # Back up past trailing whitespace
                stripped_end = content[section_start:section_end].rstrip()
                insert_pos = section_start + len(stripped_end)
                content = content[:insert_pos] + "\n" + new_entry + content[insert_pos:]
        else:
            # Section doesn't exist — add it at the end
            content = content.rstrip() + f"\n\n{section_heading}\n\n{new_entry}\n"

        # Write back
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)

        return json.dumps({"updated": True, "action": action, "index_path": index_path})

    except Exception as exc:
        return json.dumps({"error": str(exc)})
