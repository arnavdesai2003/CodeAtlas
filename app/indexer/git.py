"""Bounded, noninteractive Git subprocess execution."""
import os
import subprocess

from app.core.config import settings


REPOSITORY_ENVIRONMENT = frozenset({
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE",
})


def git_output(*args: str) -> str:
    environment = os.environ.copy()
    for name in REPOSITORY_ENVIRONMENT:
        environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, env=environment,
        timeout=settings.git_timeout_seconds,
    )
    return result.stdout.strip()
