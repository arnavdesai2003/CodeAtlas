"""Known repository failures, separate from internal implementation errors."""


class InvalidRepositoryURL(ValueError):
    pass


class RepositoryConflict(ValueError):
    pass


class RepositoryNotFound(ValueError):
    pass


class RepositoryCloneMissing(FileNotFoundError):
    pass


class RepositoryCloneFailed(RuntimeError):
    pass


class UnsafeClonePath(RuntimeError):
    pass
