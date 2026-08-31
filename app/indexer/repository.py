from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.db.models import CodeFile, Repository


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
    parsed = urlparse(clone_url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only HTTP/HTTPS GitHub URLs are supported.")

    if parsed.hostname not in {"github.com", "www.github.com"}:
        raise ValueError("Only github.com repositories are currently supported.")

    parts = [
        part
        for part in parsed.path.strip("/").split("/")
        if part
    ]

    if len(parts) != 2:
        raise ValueError(
            "GitHub URL must have the format "
            "https://github.com/owner/repository"
        )

    owner = parts[0]
    repository_name = parts[1]

    if repository_name.endswith(".git"):
        repository_name = repository_name[:-4]

    if not owner or not repository_name:
        raise ValueError("Invalid GitHub repository URL.")

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
        if not path.is_file():
            continue

        if ".git" in path.parts:
            continue

        extension = path.suffix.lower()

        language = LANGUAGE_BY_EXTENSION.get(extension)

        if language is None:
            continue

        relative_path = path.relative_to(repository_path)

        discovered_files.append(
            {
                "path": relative_path.as_posix(),
                "language": language,
                "content_hash": calculate_file_hash(path),
            }
        )

    return discovered_files


def get_current_commit(repository_path: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository_path),
            "rev-parse",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    return result.stdout.strip()


def get_current_branch(repository_path: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository_path),
            "branch",
            "--show-current",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    branch = result.stdout.strip()

    return branch or "unknown"


def ingest_repository(
    db: Session,
    clone_url: str,
) -> dict:
    owner, repository_name = parse_github_url(clone_url)

    existing_repository = (
        db.query(Repository)
        .filter(Repository.clone_url == clone_url)
        .first()
    )

    if existing_repository:
        raise ValueError("Repository has already been added.")

    repository_path = (
        REPOSITORY_ROOT
        / owner
        / repository_name
    )

    if repository_path.exists():
        raise ValueError(
            f"Repository directory already exists: {repository_path}"
        )

    repository_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                clone_url,
                str(repository_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

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

        db.commit()
        db.refresh(repository)

        return {
            "repository_id": repository.id,
            "name": repository.name,
            "owner": owner,
            "clone_url": repository.clone_url,
            "branch": repository.default_branch,
            "commit": repository.last_indexed_commit,
            "source_files_discovered": len(source_files),
            "local_path": str(repository_path),
        }

    except subprocess.CalledProcessError as exc:
        db.rollback()

        if repository_path.exists():
            shutil.rmtree(repository_path)

        error_message = (
            exc.stderr.strip()
            if exc.stderr
            else "Git clone failed."
        )

        raise RuntimeError(error_message) from exc

    except Exception:
        db.rollback()

        if repository_path.exists():
            shutil.rmtree(repository_path)

        raise