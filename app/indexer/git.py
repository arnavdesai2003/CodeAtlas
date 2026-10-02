"""Bounded, noninteractive Git subprocess execution."""
import os
import subprocess

from app.core.config import settings


def git_output(*args: str) -> str:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, env=environment,
        timeout=settings.git_timeout_seconds,
    )
    return result.stdout.strip()
