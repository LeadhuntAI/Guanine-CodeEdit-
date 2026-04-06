"""
Spark configuration management and API key handling.

Manages SparkConfig loading/saving from JSON files, with interactive
API key prompting as a fallback.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


DEFAULT_MODELS: dict[str, str] = {
    "planner": "anthropic/claude-opus-4.6",
    "explorer": "moonshotai/kimi-k2.5",
    "relationship_mapper": "anthropic/claude-opus-4.6",
    "doc_writer": "z-ai/glm-5-turbo",
    "onboarding": "anthropic/claude-opus-4.6",
    "library": "anthropic/claude-opus-4.6",
}


@dataclass
class SparkConfig:
    """Runtime configuration for a Spark documentation run."""

    api_key: str
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    iterations: int = 3
    max_concurrent_workers: int = 10
    target_dir: str = "."
    mode: str = "fresh"  # "fresh" | "fill-gaps" | "refresh"
    exclude_patterns: list[str] = field(default_factory=list)
    code_index: bool = True


def _project_config_path(target_dir: str) -> Path:
    """Return the project-level config path: <target>/.claude/spark_config.json."""
    return Path(target_dir).resolve() / ".claude" / "spark_config.json"


def _user_config_path() -> Path:
    """Return the user-level config path: ~/.config/spark/config.json."""
    return Path.home() / ".config" / "spark" / "config.json"


def _load_config_from_file(path: Path) -> Optional[SparkConfig]:
    """Attempt to load a SparkConfig from a JSON file. Returns None on failure."""
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Defaults always win for known roles (ensures model IDs stay current).
        # Saved config can add custom roles not in defaults.
        models = dict(data.get("models", {}))
        models.update(DEFAULT_MODELS)
        return SparkConfig(
            api_key=data["api_key"],
            models=models,
            iterations=data.get("iterations", 3),
            max_concurrent_workers=data.get("max_concurrent_workers", 10),
            target_dir=data.get("target_dir", "."),
            mode=data.get("mode", "fresh"),
            exclude_patterns=data.get("exclude_patterns", []),
            code_index=data.get("code_index", True),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"Warning: could not parse config at {path}: {exc}")
        return None


def validate_api_key(api_key: str) -> tuple[bool, str]:
    """Test an OpenRouter API key with a minimal call.

    Makes a cheap chat completion call (max_tokens=1) to verify the key works.
    Returns (True, "") on success or (False, error_message) on failure.
    """
    from spark.engine.openrouter import OpenRouterClient

    try:
        client = OpenRouterClient(api_key=api_key)
        client.chat_completion(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        return True, ""
    except Exception as e:
        return False, str(e)


def load_spark_init_key(target_dir: str) -> str | None:
    """Check .claude/spark_plans/spark_init.json for an API key.

    Returns the key if found and not a placeholder, else None.
    """
    init_path = os.path.join(target_dir, ".claude", "spark_plans", "spark_init.json")
    if not os.path.isfile(init_path):
        return None
    try:
        with open(init_path, "r") as f:
            data = json.load(f)
        key = data.get("openrouter_api_key", "")
        if key and not key.startswith("sk-or-v1-your-"):
            return key
    except (json.JSONDecodeError, OSError):
        pass
    return None


def prompt_for_api_key() -> str:
    """Interactively prompt the user for an OpenRouter API key.

    Keeps asking until a non-empty string is provided.
    """
    while True:
        key = input("Enter your OpenRouter API key: ").strip()
        if key:
            return key
        print("API key cannot be empty. Please try again.")


def _ensure_valid_key(api_key: str) -> str:
    """Validate an API key, re-prompting until a working key is provided."""
    print("Validating API key...")
    while True:
        success, error = validate_api_key(api_key)
        if success:
            print("API key is valid.")
            return api_key
        print(f"API key validation failed: {error}")
        api_key = prompt_for_api_key()


def save_config(config: SparkConfig, path: str) -> None:
    """Save a SparkConfig to *path* as human-readable JSON."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    print(f"Config saved to {dest}")


def load_or_create_config(target_dir: str) -> SparkConfig:
    """Load configuration, checking project-level then user-level files.

    If neither file exists, interactively prompts for an API key and
    creates a project-level config file.
    """
    # 1. Project-level config
    project_path = _project_config_path(target_dir)
    cfg = _load_config_from_file(project_path)
    if cfg is not None:
        cfg.target_dir = target_dir
        cfg.api_key = _ensure_valid_key(cfg.api_key)
        return cfg

    # 2. User-level config
    user_path = _user_config_path()
    cfg = _load_config_from_file(user_path)
    if cfg is not None:
        cfg.target_dir = target_dir
        cfg.api_key = _ensure_valid_key(cfg.api_key)
        return cfg

    # 3. Check spark_init.json
    spark_key = load_spark_init_key(target_dir)
    if spark_key:
        print("Found API key in spark_init.json.")
        spark_key = _ensure_valid_key(spark_key)
        cfg = SparkConfig(api_key=spark_key, target_dir=target_dir)
        save_config(cfg, str(project_path))
        return cfg

    # 4. Interactive fallback
    print("No Spark configuration found.")
    api_key = prompt_for_api_key()
    api_key = _ensure_valid_key(api_key)
    cfg = SparkConfig(api_key=api_key, target_dir=target_dir)
    save_config(cfg, str(project_path))
    return cfg
