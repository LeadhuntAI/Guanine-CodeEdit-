"""One-time setup: register this repo for sandboxed agent sessions
and enable the sandbox by creating the flag file."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_schema

REPO_PATH = os.path.dirname(os.path.abspath(__file__))
REPO_NAME = "Guanine(CodeEdit)"

# Register the repo
repo = agent_schema.register_repo(
    repo_path=REPO_PATH,
    repo_name=REPO_NAME,
    allowed_commands=["python", "pytest", "pip", "git"],
    allow_free_commands=True,
)

print(f"Repo registered successfully!")
print(f"  repo_id:   {repo['repo_id']}")
print(f"  repo_name: {repo['repo_name']}")
print(f"  repo_path: {repo['repo_path']}")

# Create the sandbox flag file
flag_path = os.path.join(REPO_PATH, '.claude', 'sandbox-active')
os.makedirs(os.path.dirname(flag_path), exist_ok=True)
with open(flag_path, 'w') as f:
    f.write('Sandbox is active. Remove this file to disable.\n')

print(f"\nSandbox enabled: {flag_path}")
print(f"\nSetup complete. Restart Claude Code to pick up the new configuration.")
