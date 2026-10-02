"""Bounded, noninteractive Git subprocess execution."""
import os
import subprocess
from pathlib import Path

from app.core.config import settings
from app.indexer.errors import UnsafeClonePath


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
    try:
        # Text mode's universal-newline conversion corrupts CR/CRLF bytes in
        # NUL-delimited filenames. Decode explicitly without changing them.
        result = subprocess.run(
            ["git", *args], check=True, capture_output=True, text=False,
            stdin=subprocess.DEVNULL, env=environment,
            timeout=settings.git_timeout_seconds,
        )
    except subprocess.CalledProcessError as exc:
        # Preserve the existing textual diagnostic contract for clone errors.
        if isinstance(exc.output, bytes):
            exc.output = exc.output.decode("utf-8", errors="replace")
        if isinstance(exc.stderr, bytes):
            exc.stderr = exc.stderr.decode("utf-8", errors="replace")
        raise
    return result.stdout.decode("utf-8").strip()


def validate_git_metadata(repository_path: Path) -> None:
    metadata = repository_path / ".git"
    common = metadata / "commondir"
    if metadata.is_symlink() or not metadata.is_dir() or common.exists() or common.is_symlink():
        raise UnsafeClonePath("Repository Git metadata is missing or redirected.")


def repository_git_output(repository_path: Path, *args: str) -> str:
    validate_git_metadata(repository_path)
    absolute = repository_path.absolute()
    return git_output("-C", str(absolute), "--git-dir", str(absolute / ".git"),
                      "--work-tree", str(absolute), *args)
