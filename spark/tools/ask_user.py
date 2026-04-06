"""Terminal input tool for interactive onboarding conversations."""

import json


def execute(question: str, default: str = "", hint: str = "", **kwargs) -> str:
    """Prompt the user in the terminal and return their answer as JSON.

    Args:
        question: The question to display
        default: Default value shown in brackets, returned if user presses Enter
        hint: Optional hint displayed before the question
    """
    if hint:
        print(f"  ({hint})")
    if default:
        answer = input(f"{question} [{default}]: ").strip()
        return json.dumps({"answer": answer if answer else default})
    else:
        answer = input(f"{question}: ").strip()
        return json.dumps({"answer": answer})
