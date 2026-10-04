"""Resolve a single concrete index; keep retired indices for pinned readers."""
from collections.abc import Mapping

from elasticsearch import NotFoundError
from app.search.errors import InvalidSearchResponseError
from app.search.responses import response_body


def alias_target(client, *, alias_name: str) -> str | None:
    try:
        aliases = response_body(client.indices.get_alias(name=alias_name))
    except NotFoundError:
        return None
    if not isinstance(aliases, Mapping) or len(aliases) != 1:
        raise InvalidSearchResponseError("The search alias must reference exactly one index.")
    target, metadata = next(iter(aliases.items()))
    names = metadata.get("aliases") if isinstance(metadata, Mapping) else None
    if (
        not isinstance(target, str) or not target or target.strip() != target
        or not isinstance(names, Mapping)
        or alias_name not in names or not isinstance(names[alias_name], Mapping)
    ):
        raise InvalidSearchResponseError("The search alias response was invalid.")
    return target


def resolve_active_index(client, *, alias_name: str, legacy_name: str) -> str:
    return alias_target(client, alias_name=alias_name) or legacy_name
