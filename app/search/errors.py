"""Failures that must never become successful cached search results."""


class IncompleteSearchError(RuntimeError):
    """Elasticsearch returned a timed-out or failed-shard search response."""
