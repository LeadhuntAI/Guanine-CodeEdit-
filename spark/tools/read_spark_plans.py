"""Read project plans and spark_init.json from .claude/spark_plans/."""

import glob
import json
import os


def execute(_base_dir: str = ".", **kwargs) -> str:
    """Read all .md plan files and spark_init.json from .claude/spark_plans/.

    Returns JSON with:
        plans: list of {file, content} for each .md file
        init_json: parsed spark_init.json contents or null
        count: number of .md files found
    """
    plans_dir = os.path.join(_base_dir, ".claude", "spark_plans")

    # Read .md files
    plans = []
    if os.path.isdir(plans_dir):
        for fpath in sorted(glob.glob(os.path.join(plans_dir, "*.md"))):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                plans.append({
                    "file": os.path.basename(fpath),
                    "content": content
                })
            except (OSError, UnicodeDecodeError):
                continue

    # Read spark_init.json
    init_json = None
    init_path = os.path.join(plans_dir, "spark_init.json")
    if os.path.isfile(init_path):
        try:
            with open(init_path, "r", encoding="utf-8") as f:
                init_json = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    return json.dumps({
        "plans": plans,
        "init_json": init_json,
        "count": len(plans)
    })
