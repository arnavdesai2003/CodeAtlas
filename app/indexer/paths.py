"""Source selection for a stationary, trusted clone directory."""

from pathlib import Path, PurePosixPath


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
