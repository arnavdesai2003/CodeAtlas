"""Read-only audit of registered Python files against the installed grammar."""
import argparse
import json
from pathlib import Path

from tree_sitter import Parser

from app.db.database import SessionLocal
from app.db.models import CodeFile, Repository
from app.indexer.parser import PYTHON_LANGUAGE
from app.indexer.paths import clone_directory, regular_source_path
from app.indexer.repository import REPOSITORY_ROOT, parse_github_url
from app.indexer.source import read_python_source


def inspect_source(root: Path, relative_path: str) -> str | None:
    path = regular_source_path(root, relative_path)
    if path is None:
        return "unavailable_or_excluded"
    try:
        source = read_python_source(path)
    except (OSError, UnicodeError, SyntaxError, LookupError):
        return "source_read_error"
    tree = Parser(PYTHON_LANGUAGE).parse(source.encode("utf-8"))
    return "grammar_recovery" if tree.root_node.has_error else None


def audit(db, repository_id=None, root=REPOSITORY_ROOT):
    repositories = db.query(Repository).order_by(Repository.id)
    if repository_id is not None:
        repositories = repositories.filter(Repository.id == repository_id)
    repositories = repositories.all()
    if repository_id is not None and not repositories:
        raise ValueError("Repository does not exist.")
    report = {"repositories": len(repositories), "python_files": 0, "clean_files": 0, "findings": []}
    for repository in repositories:
        owner, name = parse_github_url(repository.clone_url)
        # Unsafe clone boundaries abort rather than inspect redirected content.
        directory = clone_directory(root, owner, name)
        files = db.query(CodeFile).filter(
            CodeFile.repository_id == repository.id, CodeFile.language == "python",
        ).order_by(CodeFile.id).all()
        for file in files:
            report["python_files"] += 1
            issue = inspect_source(directory, file.path)
            if issue is None:
                report["clean_files"] += 1
            else:
                report["findings"].append({
                    "repository_id": repository.id, "file_id": file.id,
                    "path": file.path, "issue": issue,
                })
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-id", type=int)
    args = parser.parse_args()
    with SessionLocal() as db:
        report = audit(db, args.repository_id)
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
