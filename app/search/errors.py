"""Failures that must never become successful cached search results."""


class IncompleteSearchError(RuntimeError):
    """Elasticsearch returned a timed-out or failed-shard search response."""


class InvalidSearchResponseError(IncompleteSearchError):
    """Retrieval scores cannot be safely formatted or normalized."""


class InvalidQueryEmbeddingError(IncompleteSearchError):
    """Model output cannot safely be submitted as a query vector."""


class InvalidRerankerOutputError(IncompleteSearchError):
    """Reranker output does not provide one finite score per candidate."""
