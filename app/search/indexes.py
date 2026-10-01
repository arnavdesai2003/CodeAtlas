"""Resolve a single concrete index; keep retired indices for pinned readers."""
from elasticsearch import NotFoundError


def alias_target(client, *, alias_name: str) -> str | None:
    try:
        aliases = client.indices.get_alias(name=alias_name)
    except NotFoundError:
        return None
    if len(aliases) != 1:
        raise RuntimeError("The search alias must reference exactly one index.")
    return next(iter(aliases))


def resolve_active_index(client, *, alias_name: str, legacy_name: str) -> str:
    return alias_target(client, alias_name=alias_name) or legacy_name
