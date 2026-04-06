"""Install a plugin from the Spark library into the target project."""

import json
import os
import shutil
import subprocess
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile
from io import BytesIO

from spark.tools.list_library import _load_catalog
from spark.tools.install_templates import detect_platform, PLATFORM_MAP


def execute(
    plugin_id: str = "",
    config_overrides: str = "{}",
    platform: str = "auto",
    _base_dir: str = ".",
    **kwargs,
) -> str:
    """Install a plugin from the Spark library into the target project.

    Clones the plugin repo to a temp directory, copies the specified files
    into the target project, updates RULES_INDEX.md, and cleans up.

    Args:
        plugin_id: The plugin identifier from catalog.json.
        config_overrides: JSON string of config option overrides.
        platform: Target platform (auto/claude/windsurf/copilot/codex).
        _base_dir: Target repo root.
    """
    if not plugin_id:
        return json.dumps({"error": "plugin_id is required"})

    # Load catalog and find plugin
    try:
        catalog = _load_catalog()
    except (OSError, json.JSONDecodeError) as exc:
        return json.dumps({"error": f"Could not load catalog: {exc}"})

    plugin = None
    for p in catalog:
        if p.get("id") == plugin_id:
            plugin = p
            break

    if plugin is None:
        return json.dumps({"error": f"Plugin not found: {plugin_id}"})

    # Resolve platform
    if platform == "auto":
        platform = detect_platform(_base_dir)
    info = PLATFORM_MAP.get(platform, PLATFORM_MAP["claude"])
    platform_dir = info["dir"]

    # Parse config overrides
    try:
        overrides = json.loads(config_overrides) if config_overrides else {}
    except json.JSONDecodeError:
        overrides = {}

    integration = plugin.get("integration", {})
    files_to_copy = integration.get("files_to_copy", [])
    index_entries = integration.get("rules_index_entries", [])

    installed_files = []
    updated_indexes = []
    errors = []

    # Clone or download the plugin repo
    temp_dir = tempfile.mkdtemp(prefix="spark_plugin_")
    repo_url = plugin.get("repo_url", "")

    try:
        clone_ok = _clone_repo(repo_url, temp_dir)
        if not clone_ok:
            errors.append("Could not clone or download plugin repo")
            return json.dumps({
                "plugin_id": plugin_id,
                "installed_files": [],
                "updated_indexes": [],
                "config_applied": overrides,
                "errors": errors,
            })

        # Copy files from cloned repo into target project
        for entry in files_to_copy:
            src_rel = entry.get("src", "")
            dst_rel = entry.get("dst", "")
            if not src_rel or not dst_rel:
                continue

            src_path = os.path.join(temp_dir, src_rel)
            # Resolve {platform_dir} in destination
            dst_rel_resolved = dst_rel.replace("{platform_dir}", platform_dir)
            dst_path = os.path.join(_base_dir, platform_dir, dst_rel_resolved)

            if not os.path.isfile(src_path):
                errors.append(f"Source file not found in plugin repo: {src_rel}")
                continue

            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy2(src_path, dst_path)
            installed_files.append(os.path.join(platform_dir, dst_rel_resolved))

        # Update RULES_INDEX.md for each entry
        from spark.tools.update_rules_index import execute as update_index

        for entry in index_entries:
            entry_path = entry.get("entry_path", "")
            summary = entry.get("summary", "")
            section = entry.get("section", "Plugin Rules")

            if not entry_path or not summary:
                continue

            index_path = os.path.join(platform_dir, "RULES_INDEX.md")
            result_str = update_index(
                index_path=index_path,
                entry_path=entry_path,
                summary=summary,
                section=section,
                _base_dir=_base_dir,
            )
            result = json.loads(result_str)
            if result.get("error"):
                errors.append(f"Failed to update index: {result['error']}")
            else:
                updated_indexes.append({"index": index_path, "entry": entry_path})

        # Write MCP config if plugin provides one
        mcp_config = integration.get("mcp_config")
        if mcp_config and platform == "claude":
            settings_path = os.path.join(_base_dir, ".claude", "settings.json")
            settings = {}
            if os.path.isfile(settings_path):
                try:
                    with open(settings_path, "r", encoding="utf-8") as f:
                        settings = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass
            mcp_servers = settings.setdefault("mcpServers", {})
            mcp_servers.update(mcp_config)
            try:
                os.makedirs(os.path.dirname(settings_path), exist_ok=True)
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(settings, f, indent=2)
                installed_files.append(".claude/settings.json")
            except OSError as exc:
                errors.append(f"Failed to write MCP config: {exc}")

    finally:
        # Clean up temp directory
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    return json.dumps({
        "plugin_id": plugin_id,
        "plugin_name": plugin.get("name", ""),
        "platform": platform,
        "installed_files": installed_files,
        "updated_indexes": updated_indexes,
        "config_applied": overrides,
        "post_install_message": integration.get("post_install_message", ""),
        "errors": errors,
    })


def _clone_repo(repo_url: str, dest_dir: str) -> bool:
    """Clone a git repo to dest_dir. Falls back to zip download on failure."""
    if not repo_url:
        return False

    # Try git clone first
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, dest_dir],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: try downloading zip from GitHub
    if "github.com" in repo_url:
        return _download_github_zip(repo_url, dest_dir)

    return False


def _download_github_zip(repo_url: str, dest_dir: str) -> bool:
    """Download a GitHub repo as a zip archive."""
    # Convert git URL to zip URL
    # https://github.com/user/repo.git → https://github.com/user/repo/archive/refs/heads/main.zip
    clean_url = repo_url.rstrip("/").removesuffix(".git")
    zip_url = f"{clean_url}/archive/refs/heads/main.zip"

    try:
        resp = urlopen(zip_url, timeout=60)
        data = resp.read()
        with ZipFile(BytesIO(data)) as zf:
            # GitHub zips have a top-level directory like "repo-main/"
            # Extract contents into dest_dir
            top_dirs = {name.split("/")[0] for name in zf.namelist() if "/" in name}
            prefix = top_dirs.pop() + "/" if len(top_dirs) == 1 else ""

            for member in zf.namelist():
                if not member.startswith(prefix):
                    continue
                rel_path = member[len(prefix):]
                if not rel_path:
                    continue
                out_path = os.path.join(dest_dir, rel_path)
                if member.endswith("/"):
                    os.makedirs(out_path, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(out_path), exist_ok=True)
                    with open(out_path, "wb") as f:
                        f.write(zf.read(member))
        return True
    except Exception:
        return False
