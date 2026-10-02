"""Source selection for a stationary, trusted clone directory."""

from pathlib import Path, PurePosixPath
from app.indexer.errors import UnsafeClonePath


def clone_directory(root: Path, owner: str, repository: str) -> Path:
    """Reject redirected/non-directory components at and below a trusted root.

    Missing components are allowed for ingestion's atomic reservation. Ancestors
    above the configured root and concurrent replacement remain operator-owned.
    """
    for component in (owner, repository):
        if component in {"", ".", ".."} or "/" in component or "\\" in component:
            raise UnsafeClonePath("Unsafe repository directory component.")
    for path in (root, root / owner, root / owner / repository):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise UnsafeClonePath("Repository directory is redirected or not a directory.")
    return root / owner / repository


def regular_source_path(root: Path, relative: str) -> Path | None:
    """Select regular files without traversing links inside the clone.

    This is a selection check, not protection against concurrent filesystem
    replacement. Indexing requires exclusive ownership of generated clones.
    """
    name = PurePosixPath(relative)
    if name.is_absolute() or not name.parts or ".." in name.parts or ".git" in name.parts:
        return None
    if root.is_symlink():
        return None
    path = root
    for part in name.parts:
        path = path / part
        if path.is_symlink():
            return None
    return path if path.is_file() else None
