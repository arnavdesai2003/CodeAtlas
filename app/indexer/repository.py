from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.db.models import CodeFile, Repository
from app.indexer.errors import InvalidRepositoryURL, RepositoryConflict, RepositoryCloneFailed
from app.indexer.paths import regular_source_path, clone_directory
from app.indexer.git import git_output, repository_git_output


REPOSITORY_ROOT = Path("data/repos")


LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".sql": "sql",
}


def parse_github_url(clone_url: str) -> tuple[str, str]:
    # urlparse silently strips some controls; reject before parsing so the
    # accepted URL is the same text passed to Git and stored in metadata.
    if not isinstance(clone_url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in clone_url):
        raise InvalidRepositoryURL("Invalid GitHub repository URL.")
    try:
        parsed = urlparse(clone_url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise InvalidRepositoryURL("Invalid GitHub repository URL.") from exc

    if parsed.scheme not in {"http", "https"}:
        raise InvalidRepositoryURL("Only HTTP/HTTPS GitHub URLs are supported.")

    if hostname not in {"github.com", "www.github.com"}:
        raise InvalidRepositoryURL("Only github.com repositories are currently supported.")

    if parsed.netloc.lower() not in {"github.com", "www.github.com"} or "?" in clone_url or "#" in clone_url:
        raise InvalidRepositoryURL("Credentials, ports, query strings and fragments are not supported.")

    match = re.fullmatch(r"/([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+)/?", parsed.path)

    if match is None:
        raise InvalidRepositoryURL(
            "GitHub URL must have the format "
            "https://github.com/owner/repository"
        )

    owner, repository_name = match.groups()

    if repository_name.endswith(".git"):
        repository_name = repository_name[:-4]

    if not owner or not repository_name:
        raise InvalidRepositoryURL("Invalid GitHub repository URL.")
    if owner in {".", ".."} or repository_name in {".", ".."}:
        raise InvalidRepositoryURL("Invalid GitHub repository path component.")

    return owner, repository_name


def calculate_file_hash(path: Path) -> str:
    sha256 = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(lambda: file.read(65536), b""):
            sha256.update(block)

    return sha256.hexdigest()


def discover_source_files(repository_path: Path) -> list[dict]:
    discovered_files = []

    for path in repository_path.rglob("*"):
        relative_path = path.relative_to(repository_path)
        if regular_source_path(repository_path, relative_path.as_posix()) is None:
            continue

        extension = path.suffix.lower()

        language = LANGUAGE_BY_EXTENSION.get(extension)

        if language is None:
            continue

        discovered_files.append(
            {
                "path": relative_path.as_posix(),
                "language": language,
                "content_hash": calculate_file_hash(path),
            }
        )

    return discovered_files


def get_current_commit(repository_path: Path) -> str:
    return repository_git_output(repository_path, "rev-parse", "HEAD")


def get_current_branch(repository_path: Path) -> str:
    branch = repository_git_output(repository_path, "branch", "--show-current")

    return branch or "unknown"


def ingest_repository(
    db: Session,
    clone_url: str,
) -> dict:
    owner, repository_name = parse_github_url(clone_url)
    repository_path = clone_directory(REPOSITORY_ROOT, owner, repository_name)

    existing_repository = (
        db.query(Repository)
        .filter(Repository.clone_url == clone_url)
        .first()
    )

    if existing_repository:
        raise RepositoryConflict("Repository has already been added.")

    if repository_path.exists():
        raise RepositoryConflict(
            f"Repository directory already exists: {repository_path}"
        )

    repository_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Reserve the destination atomically. A losing concurrent request must
    # never clean up a directory that another ingestion owns.
    try:
        repository_path.mkdir()
    except FileExistsError as exc:
        raise RepositoryConflict(f"Repository directory already exists: {repository_path}") from exc

    commit_started = False

    try:
        git_output("clone", "--depth", "1", clone_url, str(repository_path))

        commit_sha = get_current_commit(repository_path)
        branch = get_current_branch(repository_path)

        source_files = discover_source_files(repository_path)

        repository = Repository(
            name=repository_name,
            clone_url=clone_url,
            default_branch=branch,
            last_indexed_commit=commit_sha,
        )

        db.add(repository)
        db.flush()

        for source_file in source_files:
            db.add(
                CodeFile(
                    repository_id=repository.id,
                    path=source_file["path"],
                    language=source_file["language"],
                    content_hash=source_file["content_hash"],
                    last_indexed_commit=commit_sha,
                )
            )

        # Build the response before commit: no fallible database reads after a
        # successful commit may trigger cleanup of the registered clone.
        result = {
            "repository_id": repository.id,
            "name": repository.name,
            "owner": owner,
            "clone_url": repository.clone_url,
            "branch": repository.default_branch,
            "commit": repository.last_indexed_commit,
            "source_files_discovered": len(source_files),
            "local_path": str(repository_path),
        }
        commit_started = True
        db.commit()
        return result

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        db.rollback()

        if not commit_started and repository_path.exists():
            shutil.rmtree(repository_path)

        error_message = "Git operation timed out." if isinstance(exc, subprocess.TimeoutExpired) else (
            exc.stderr.strip() if exc.stderr else "Git clone failed."
        )

        raise RepositoryCloneFailed(error_message) from exc

    except Exception:
        db.rollback()

        # A lost commit acknowledgement has an ambiguous outcome. Preserve
        # the clone rather than deleting files for a possibly committed row.
        if not commit_started and repository_path.exists():
            shutil.rmtree(repository_path)

        raise
