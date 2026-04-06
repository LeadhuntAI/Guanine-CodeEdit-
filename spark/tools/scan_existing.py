"""Scan target repo for existing platform files, rules, and skills."""

import json

from spark.tools.install_templates import detect_platform, scan_existing as _scan


def execute(target_dir: str = ".", platform: str = "auto", **kwargs) -> str:
    """Scan target repo and return what already exists.

    Returns JSON with platform info, existing rules/skills lists,
    and flags for has_instructions_file, has_rules, has_skills, etc.
    """
    target_dir = kwargs.get("_base_dir", target_dir)
    if platform == "auto":
        platform = detect_platform(target_dir)
    return json.dumps(_scan(target_dir, platform))
