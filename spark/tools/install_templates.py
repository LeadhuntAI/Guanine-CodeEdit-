"""Install Agent Blueprint templates into a target repository."""

import json
import os
import shutil


# Platform directory and instructions-file mappings.
PLATFORM_MAP = {
    "claude":    {"dir": ".claude",    "instructions": "CLAUDE.md"},
    "windsurf":  {"dir": ".windsurf",  "instructions": "AGENTS.md"},
    "copilot":   {"dir": ".github",    "instructions": "AGENTS.md"},
    "codex":     {"dir": ".codex",     "instructions": "AGENTS.md"},
}


def detect_platform(target_dir: str) -> str:
    """Auto-detect platform from existing directories in the target repo.

    Returns the first match, preferring claude.  Falls back to 'claude'.
    """
    for platform, info in PLATFORM_MAP.items():
        if os.path.isdir(os.path.join(target_dir, info["dir"])):
            return platform
    return "claude"


def scan_existing(target_dir: str, platform: str = "claude") -> dict:
    """Check what already exists in the target repo.

    Returns a dict describing existing files so the caller can decide
    what to overwrite vs skip.
    """
    info = PLATFORM_MAP.get(platform, PLATFORM_MAP["claude"])
    plat_dir = os.path.join(target_dir, info["dir"])
    instr_file = os.path.join(target_dir, info["instructions"])

    existing = {
        "platform": platform,
        "platform_dir": info["dir"],
        "instructions_file": info["instructions"],
        "has_platform_dir": os.path.isdir(plat_dir),
        "has_instructions_file": os.path.isfile(instr_file),
        "has_rules": os.path.isdir(os.path.join(plat_dir, "rules")),
        "has_skills": os.path.isdir(os.path.join(plat_dir, "skills")),
        "has_agentic": os.path.isdir(os.path.join(target_dir, "agentic")),
        "existing_rules": [],
        "existing_skills": [],
    }

    # List existing rule files
    rules_dir = os.path.join(plat_dir, "rules")
    if os.path.isdir(rules_dir):
        for dirpath, _dirnames, filenames in os.walk(rules_dir):
            for fname in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fname), plat_dir)
                existing["existing_rules"].append(rel)

    # List existing skill files
    skills_dir = os.path.join(plat_dir, "skills")
    if os.path.isdir(skills_dir):
        for dirpath, _dirnames, filenames in os.walk(skills_dir):
            for fname in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fname), plat_dir)
                existing["existing_skills"].append(rel)

    return existing


def execute(
    target_dir: str = ".",
    platform: str = "auto",
    overwrite_instructions: bool = False,
    overwrite_rules: bool = False,
    **kwargs,
) -> str:
    """Install Agent Blueprint templates into target directory.

    Args:
        target_dir: Where to install.
        platform: One of 'claude', 'windsurf', 'copilot', 'codex', or
                  'auto' to detect from existing directories.
        overwrite_instructions: If True, overwrite the existing CLAUDE.md /
                                AGENTS.md instead of skipping.
        overwrite_rules: If True, overwrite existing rule and skill files.

    Returns JSON with installed, skipped, and overwritten file lists.
    """
    # Find the templates directory (inside spark/templates/)
    spark_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    templates_dir = os.path.join(spark_dir, "templates")

    if not os.path.isdir(templates_dir):
        return json.dumps({"error": f"Templates directory not found: {templates_dir}"})

    target = os.path.abspath(target_dir)

    # Resolve platform
    if platform == "auto":
        platform = detect_platform(target)
    info = PLATFORM_MAP.get(platform, PLATFORM_MAP["claude"])

    installed = []
    skipped = []
    overwritten = []

    # ---- Platform directory (.claude/ → .windsurf/ etc.) ----
    src_claude = os.path.join(templates_dir, ".claude")
    dst_plat = os.path.join(target, info["dir"])
    if os.path.isdir(src_claude):
        _copy_tree(src_claude, dst_plat, installed, skipped, overwritten,
                   overwrite=overwrite_rules)

    # ---- Instructions file (CLAUDE.md → AGENTS.md etc.) ----
    src_instr = os.path.join(templates_dir, "CLAUDE.md")
    dst_instr = os.path.join(target, info["instructions"])
    if os.path.isfile(src_instr):
        if os.path.exists(dst_instr):
            if overwrite_instructions:
                shutil.copy2(src_instr, dst_instr)
                overwritten.append(info["instructions"])
            else:
                skipped.append(info["instructions"])
        else:
            shutil.copy2(src_instr, dst_instr)
            installed.append(info["instructions"])

    # ---- Agentic engine ----
    src_agentic = os.path.join(templates_dir, "agentic")
    dst_agentic = os.path.join(target, "agentic")
    if os.path.isdir(src_agentic):
        _copy_tree(src_agentic, dst_agentic, installed, skipped, overwritten,
                   overwrite=False)  # never auto-overwrite agentic engine

    # ---- spark_plans directory ----
    plans_dir = os.path.join(target, info["dir"], "spark_plans")
    os.makedirs(plans_dir, exist_ok=True)
    if not any("spark_plans" in p for p in installed):
        installed.append(f"{info['dir']}/spark_plans/ (created)")

    return json.dumps({
        "platform": platform,
        "platform_dir": info["dir"],
        "instructions_file": info["instructions"],
        "installed": installed,
        "skipped": skipped,
        "overwritten": overwritten,
        "target_dir": target,
    })


def _copy_tree(
    src: str,
    dst: str,
    installed: list,
    skipped: list,
    overwritten: list,
    overwrite: bool = False,
) -> None:
    """Recursively copy *src* into *dst*.

    *src* and *dst* may have different basenames (e.g. ``.claude`` → ``.windsurf``).
    When *overwrite* is False, existing files are skipped.
    When *overwrite* is True, existing files are replaced and
    recorded in *overwritten*.
    """
    # dst's parent is where relative paths are anchored
    dst_root = os.path.dirname(dst)
    dst_basename = os.path.basename(dst)

    for dirpath, _dirnames, filenames in os.walk(src):
        # Map source structure onto destination structure
        rel_from_src = os.path.relpath(dirpath, src)  # e.g. "." or "rules/docs"
        if rel_from_src == ".":
            target_dir = dst
        else:
            target_dir = os.path.join(dst, rel_from_src)
        os.makedirs(target_dir, exist_ok=True)

        for fname in filenames:
            src_file = os.path.join(dirpath, fname)
            dst_file = os.path.join(target_dir, fname)
            rel_path = os.path.relpath(dst_file, dst_root)

            if os.path.exists(dst_file):
                if overwrite:
                    shutil.copy2(src_file, dst_file)
                    overwritten.append(rel_path)
                else:
                    skipped.append(rel_path)
            else:
                shutil.copy2(src_file, dst_file)
                installed.append(rel_path)
