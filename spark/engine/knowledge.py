"""
Knowledge resolution — frontmatter extraction, rule / skill discovery.

Reads markdown files, extracts lightweight YAML-subset frontmatter, and
assembles knowledge blocks (rules text, rules index, skills index) for
injection into agentic prompts.

No PyYAML dependency — the frontmatter parser handles simple ``key: value``
pairs only (strings, with optional quoting).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frontmatter extraction
# ---------------------------------------------------------------------------

def extract_frontmatter(file_path: str) -> dict:
    """Parse YAML-subset frontmatter from a markdown file.

    Supports the standard ``---`` delimiters::

        ---
        name: My Rule
        description: Does something useful
        ---

    For files without frontmatter the function falls back to:
    * First ``# heading`` as ``name``
    * First non-empty paragraph as ``description``

    Returns a dict that always contains at least ``name`` and ``description``
    keys (possibly empty strings).
    """
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Cannot read %s: %s", file_path, exc)
        return {"name": "", "description": ""}

    meta = _parse_frontmatter_block(text)
    if meta:
        meta.setdefault("name", "")
        meta.setdefault("description", "")
        return meta

    # Fallback heuristics
    return _heuristic_meta(text, file_path)


def _parse_frontmatter_block(text: str) -> dict | None:
    """Extract a ``---`` delimited frontmatter block and parse key: value lines."""
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return None

    block = match.group(1)
    result: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\w[\w\-_]*)\s*:\s*(.*)", line)
        if m:
            key = m.group(1)
            value = m.group(2).strip().strip("'\"")
            result[key] = value
    return result if result else None


def _heuristic_meta(text: str, file_path: str) -> dict:
    """Derive name/description from heading + first paragraph."""
    name = ""
    description = ""

    heading_match = re.search(r"^#\s+(.+)", text, re.MULTILINE)
    if heading_match:
        name = heading_match.group(1).strip()

    # First non-empty, non-heading paragraph
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if para and not para.startswith("#"):
            description = para[:200]
            break

    if not name:
        name = Path(file_path).stem

    return {"name": name, "description": description}


# ---------------------------------------------------------------------------
# Index builders
# ---------------------------------------------------------------------------

def build_rules_index(file_paths: list[str]) -> str:
    """Build an XML-wrapped rules index from a list of markdown file paths.

    Returns a string like::

        <AVAILABLE_RULES>
        - Rule Name: Short description
        </AVAILABLE_RULES>
    """
    entries: list[str] = []
    for fp in file_paths:
        meta = extract_frontmatter(fp)
        name = meta.get("name", Path(fp).stem)
        desc = meta.get("description", "")
        entries.append(f"- {name}: {desc}")
    body = "\n".join(entries) if entries else "- (none)"
    return f"<AVAILABLE_RULES>\n{body}\n</AVAILABLE_RULES>"


def build_skills_index(metadata: list[dict]) -> str:
    """Build an XML-wrapped skills index from a list of skill metadata dicts.

    Each dict should have at least ``name`` and ``description`` keys.

    Returns a string like::

        <AVAILABLE_SKILLS>
        - Skill Name: Short description
        </AVAILABLE_SKILLS>
    """
    entries: list[str] = []
    for item in metadata:
        name = item.get("name", "unknown")
        desc = item.get("description", "")
        entries.append(f"- {name}: {desc}")
    body = "\n".join(entries) if entries else "- (none)"
    return f"<AVAILABLE_SKILLS>\n{body}\n</AVAILABLE_SKILLS>"


# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------

def discover_skills(base_dir: str) -> list[dict]:
    """Scan for skills under *base_dir*.

    Looks in two locations:
    * ``skills/*/SKILL.md``   → folder-based skills
    * ``skill_definitions/*.json`` → JSON-defined skills

    Returns a list of dicts with keys:
    ``name``, ``description``, ``path``, ``type`` ("folder" | "json").
    """
    results: list[dict] = []
    base = Path(base_dir)

    # Folder-based skills
    skills_dir = base / "skills"
    if skills_dir.is_dir():
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            meta = extract_frontmatter(str(skill_md))
            results.append(
                {
                    "name": meta.get("name") or skill_md.parent.name,
                    "description": meta.get("description", ""),
                    "path": str(skill_md),
                    "type": "folder",
                }
            )

    # JSON-defined skills
    json_dir = base / "skill_definitions"
    if json_dir.is_dir():
        for json_file in sorted(json_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                results.append(
                    {
                        "name": data.get("name", json_file.stem),
                        "description": data.get("description", ""),
                        "path": str(json_file),
                        "type": "json",
                    }
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Bad skill JSON %s: %s", json_file, exc)

    return results


# ---------------------------------------------------------------------------
# Knowledge resolution
# ---------------------------------------------------------------------------

def resolve_knowledge(
    rules: list[str],
    skills: list[str],
    knowledge_set: list[str],
    base_dir: str,
) -> dict:
    """Assemble knowledge artefacts for injection into an agentic prompt.

    Parameters
    ----------
    rules : list[str]
        File paths to rule documents — read in full and concatenated.
    skills : list[str]
        Skill names to include (matched against discovered skills).
    knowledge_set : list[str]
        File/dir paths — extract frontmatter summaries, build an index.
    base_dir : str
        Root directory for relative path resolution and skill discovery.

    Returns
    -------
    dict with keys ``rules_text``, ``rules_index``, ``skills_index``.
    """
    base = Path(base_dir)

    # --- Rules text (full content) ---
    rules_parts: list[str] = []
    resolved_rule_paths: list[str] = []
    for rule_path in rules:
        rp = Path(rule_path) if os.path.isabs(rule_path) else base / rule_path
        if rp.is_file():
            try:
                rules_parts.append(rp.read_text(encoding="utf-8"))
                resolved_rule_paths.append(str(rp))
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("Cannot read rule %s: %s", rp, exc)

    rules_text = "\n\n".join(rules_parts)

    # --- Knowledge-set index (frontmatter summaries) ---
    knowledge_paths: list[str] = []
    for kp in knowledge_set:
        p = Path(kp) if os.path.isabs(kp) else base / kp
        if p.is_file():
            knowledge_paths.append(str(p))
        elif p.is_dir():
            for md in sorted(p.rglob("*.md")):
                knowledge_paths.append(str(md))

    # Combine rule paths + knowledge paths for the rules index
    all_rule_paths = resolved_rule_paths + knowledge_paths
    rules_index = build_rules_index(all_rule_paths) if all_rule_paths else ""

    # --- Skills index ---
    all_skills = discover_skills(base_dir)
    if skills:
        skill_names_lower = {s.lower() for s in skills}
        filtered = [s for s in all_skills if s["name"].lower() in skill_names_lower]
    else:
        filtered = all_skills

    skills_index = build_skills_index(filtered) if filtered else ""

    return {
        "rules_text": rules_text,
        "rules_index": rules_index,
        "skills_index": skills_index,
    }
