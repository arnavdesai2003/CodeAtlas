from __future__ import annotations
import math
from collections.abc import Mapping

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from sqlalchemy.orm import Session
from concurrent.futures import ThreadPoolExecutor
from app.core.clients import elasticsearch_client
from app.core.config import settings
from app.db.models import (
    CodeFile, CodeSymbol, Repository, RepositorySyncJob,
)
from app.indexer.locking import repository_sync_lock
from app.search.indexes import resolve_active_index
from app.search.embeddings import (
    EMBEDDING_DIMS,
    build_symbol_embedding_text,
    embed_text,
    embed_texts,
)
from app.search.reranker import rerank_results
from app.search.errors import IncompleteSearchError, InvalidSearchResponseError, InvalidQueryEmbeddingError

def refresh_symbol_index(index_name: str, *, client=None) -> None:
    response = (client if client is not None else elasticsearch_client).indices.refresh(index=index_name)
    shards = response.get("_shards") if isinstance(response, Mapping) else None
    failed = shards.get("failed") if isinstance(shards, Mapping) else None
    if type(failed) is not int or failed != 0:
        raise RuntimeError("Elasticsearch refresh was incomplete; retry indexing.")


def validate_index_embeddings(embeddings, expected_count: int) -> None:
    if len(embeddings) != expected_count:
        raise RuntimeError("Embedding count does not match symbol count.")
    for vector in embeddings:
        validate_embedding_vector(vector)


def validate_embedding_vector(vector) -> None:
    if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMS:
        raise RuntimeError("Embedding has invalid dimensions.")
    for value in vector:
        if type(value) not in (int, float):
            raise RuntimeError("Embedding contains invalid numeric values.")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise RuntimeError("Embedding contains invalid numeric values.")


def reranked_hybrid_search(
    query: str,
    limit: int = 10,
) -> list[dict]:
    candidate_limit = max(
        limit * 3,
        30,
    )

    candidates = hybrid_search_weighted(
        query=query,
        limit=candidate_limit,
        semantic_weight=(
            settings.hybrid_semantic_weight
        ),
    )

    return rerank_results(
        query=query,
        results=candidates,
        limit=limit,
    )

INDEX_NAME = "codeatlas_symbols"  # Legacy physical index; retained after migration.
SEARCH_ALIAS = "codeatlas_symbols_active"


def resolve_search_index() -> str:
    return resolve_active_index(
        elasticsearch_client, alias_name=SEARCH_ALIAS, legacy_name=INDEX_NAME,
    )


def _search_client() -> Elasticsearch:
    client = elasticsearch_client.options(request_timeout=60)
    if settings.elasticsearch_close_search_connections:
        # Opt-in workaround for measured response/keep-alive delays on the
        # local Docker transport. Alias resolution and writers retain pooling.
        client = client.options(headers={"connection": "close"})
    return client


# ---------------------------------------------------------------------
# Test-code detection
# ---------------------------------------------------------------------

TEST_QUERY_TERMS = {
    "test",
    "tests",
    "testing",
    "pytest",
    "unittest",
    "unit test",
    "integration test",
    "sanity check",
}


def is_test_path(path: str) -> bool:
    """
    Determine whether a source file belongs to a test suite.
    """

    normalized = path.replace("\\", "/").lower()

    filename = normalized.rsplit("/", 1)[-1]

    return (
        normalized.startswith("test/")
        or normalized.startswith("tests/")
        or "/test/" in normalized
        or "/tests/" in normalized
        or filename.startswith("test_")
        or filename.endswith("_test.py")
    )


def query_has_test_intent(query: str) -> bool:
    """
    Determine whether the user is intentionally searching for tests.
    """

    normalized = query.lower()

    return any(
        term in normalized
        for term in TEST_QUERY_TERMS
    )


# ---------------------------------------------------------------------
# Elasticsearch index creation
# ---------------------------------------------------------------------

def create_symbol_index(
    client: Elasticsearch = elasticsearch_client,
    *,
    index_name: str | None = None,
) -> None:
    """
    Provision the legacy index; require published generations to exist.
    """

    index_name = index_name or resolve_active_index(
        client, alias_name=SEARCH_ALIAS, legacy_name=INDEX_NAME,
    )
    if client.indices.exists(index=index_name):
        return
    if index_name != INDEX_NAME:
        raise RuntimeError("Published Elasticsearch generation is missing; reconcile before indexing.")

    response = client.indices.create(
        index=index_name,
        settings={
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
        mappings={
            "properties": {
                "repository_id": {
                    "type": "integer",
                },

                "repository": {
                    "type": "keyword",
                },

                "file_id": {
                    "type": "integer",
                },

                "path": {
                    "type": "text",
                    "fields": {
                        "keyword": {
                            "type": "keyword",
                        }
                    },
                },

                "language": {
                    "type": "keyword",
                },

                "is_test": {
                    "type": "boolean",
                },

                "symbol_id": {
                    "type": "integer",
                },

                "name": {
                    "type": "text",
                    "fields": {
                        "keyword": {
                            "type": "keyword",
                        }
                    },
                },

                "qualified_name": {
                    "type": "text",
                },

                "kind": {
                    "type": "keyword",
                },

                "start_line": {
                    "type": "integer",
                },

                "end_line": {
                    "type": "integer",
                },

                "code": {
                    "type": "text",
                },

                "embedding": {
                    "type": "dense_vector",
                    "dims": EMBEDDING_DIMS,
                    "index": True,
                    "similarity": "cosine",
                },
            }
        },
    )
    if (
        not isinstance(response, Mapping)
        or response.get("acknowledged") is not True
        or response.get("shards_acknowledged") is not True
    ):
        raise RuntimeError("Elasticsearch index creation was not acknowledged; retry indexing.")


# ---------------------------------------------------------------------
# Full repository indexing
# ---------------------------------------------------------------------

def index_repository_in_elasticsearch(db: Session, repository_id: int) -> dict:
    from app.search.publication import publish_repository_index
    try:
        with repository_sync_lock(db, repository_id, publication=True):
            return publish_repository_index(db, repository_id)
    except Exception:
        db.rollback()
        raise


def _write_repository_index(db: Session, repository_id: int, *, index_name: str) -> dict:
    """
    Index all parsed symbols from one repository.

    Semantic embeddings are generated in batches.
    """

    if db.get(RepositorySyncJob, repository_id) is not None:
        raise RuntimeError("Finish pending repository synchronization before full Elasticsearch indexing.")

    repository = db.get(
        Repository,
        repository_id,
    )

    if repository is None:
        raise ValueError(
            f"Repository {repository_id} does not exist."
        )

    rows = (
        db.query(
            CodeSymbol,
            CodeFile,
        )
        .join(
            CodeFile,
            CodeSymbol.file_id == CodeFile.id,
        )
        .filter(
            CodeSymbol.repository_id == repository_id
        )
        .order_by(CodeSymbol.id)
        .all()
    )

    print(
        f"PostgreSQL symbols found: {len(rows)}"
    )

    if not rows:
        return {
            "repository_id": repository.id,
            "repository": repository.name,
            "symbols_indexed": 0,
            "index": index_name,
        }

    # ---------------------------------------------------------------
    # Build semantic-document representations.
    # ---------------------------------------------------------------

    embedding_texts = [
        build_symbol_embedding_text(
            repository=repository.name,
            path=code_file.path,
            language=code_file.language,
            qualified_name=symbol.qualified_name,
            kind=symbol.kind,
            code=symbol.code,
        )
        for symbol, code_file in rows
    ]

    print(
        f"Generating {len(embedding_texts)} embeddings..."
    )

    embeddings = embed_texts(
        embedding_texts,
        batch_size=32,
    )

    validate_index_embeddings(embeddings, len(rows))

    # ---------------------------------------------------------------
    # Build Elasticsearch bulk operations.
    # ---------------------------------------------------------------

    actions = []

    for (
        (symbol, code_file),
        embedding,
    ) in zip(
        rows,
        embeddings,
    ):
        actions.append(
            {
                "_index": index_name,
                "_id": str(symbol.id),

                "_source": {
                    "repository_id": repository.id,
                    "repository": repository.name,

                    "file_id": code_file.id,
                    "path": code_file.path,
                    "language": code_file.language,

                    "is_test": is_test_path(
                        code_file.path
                    ),

                    "symbol_id": symbol.id,
                    "name": symbol.name,
                    "qualified_name": (
                        symbol.qualified_name
                    ),
                    "kind": symbol.kind,

                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,

                    "code": symbol.code,
                    "embedding": embedding,
                },
            }
        )

    print(
        f"Elasticsearch actions prepared: "
        f"{len(actions)}"
    )

    if actions:
        succeeded, errors = bulk(
            elasticsearch_client.options(
                request_timeout=60
            ),
            actions,
        )

        if errors or succeeded != len(actions):
            raise RuntimeError("Full Elasticsearch bulk indexing was incomplete; retry full indexing.")

        refresh_symbol_index(index_name)

    return {
        "repository_id": repository.id,
        "repository": repository.name,
        "symbols_indexed": len(actions),
        "index": index_name,
    }


# ---------------------------------------------------------------------
# Incremental deletion
# ---------------------------------------------------------------------

def delete_paths_from_elasticsearch(
    repository_id: int,
    paths: list[str],
) -> None:
    """
    Delete documents belonging to changed/deleted source files.

    Used before incremental re-indexing.
    """

    if not paths:
        return

    index_name = resolve_search_index()
    create_symbol_index(index_name=index_name)

    response = elasticsearch_client.options(
        request_timeout=60
    ).delete_by_query(
        index=index_name,
        query={
            "bool": {
                "filter": [
                    {
                        "term": {
                            "repository_id": (
                                repository_id
                            )
                        }
                    },
                    {
                        "terms": {
                            "path.keyword": paths
                        }
                    },
                ]
            }
        },
        conflicts="proceed",
        refresh=True,
    )
    total = response.get("total") if isinstance(response, Mapping) else None
    deleted = response.get("deleted") if isinstance(response, Mapping) else None
    conflicts = response.get("version_conflicts") if isinstance(response, Mapping) else None
    failures = response.get("failures") if isinstance(response, Mapping) else None
    if (
        not isinstance(response, Mapping)
        or response.get("timed_out") is not False
        or type(total) is not int or total < 0
        or type(deleted) is not int or deleted < 0 or deleted != total
        or type(conflicts) is not int or conflicts != 0
        or not isinstance(failures, list) or failures
    ):
        raise RuntimeError("Elasticsearch path deletion was incomplete; retry synchronization.")


# ---------------------------------------------------------------------
# Incremental file indexing
# ---------------------------------------------------------------------

def index_files_in_elasticsearch(
    db: Session,
    file_ids: list[int],
) -> int:
    """
    Index only specific changed files.

    Used by incremental Git synchronization.
    """

    if not file_ids:
        return 0

    index_name = resolve_search_index()
    create_symbol_index(index_name=index_name)

    rows = (
        db.query(
            CodeSymbol,
            CodeFile,
            Repository,
        )
        .join(
            CodeFile,
            CodeSymbol.file_id == CodeFile.id,
        )
        .join(
            Repository,
            CodeSymbol.repository_id
            == Repository.id,
        )
        .filter(
            CodeFile.id.in_(file_ids)
        )
        .order_by(CodeSymbol.id)
        .all()
    )

    if not rows:
        return 0

    embedding_texts = [
        build_symbol_embedding_text(
            repository=repository.name,
            path=code_file.path,
            language=code_file.language,
            qualified_name=symbol.qualified_name,
            kind=symbol.kind,
            code=symbol.code,
        )
        for (
            symbol,
            code_file,
            repository,
        ) in rows
    ]

    embeddings = embed_texts(
        embedding_texts,
        batch_size=32,
    )

    validate_index_embeddings(embeddings, len(rows))

    actions = []

    for (
        (
            symbol,
            code_file,
            repository,
        ),
        embedding,
    ) in zip(
        rows,
        embeddings,
    ):
        actions.append(
            {
                "_index": index_name,
                "_id": str(symbol.id),

                "_source": {
                    "repository_id": repository.id,
                    "repository": repository.name,

                    "file_id": code_file.id,
                    "path": code_file.path,
                    "language": code_file.language,

                    "is_test": is_test_path(
                        code_file.path
                    ),

                    "symbol_id": symbol.id,
                    "name": symbol.name,
                    "qualified_name": (
                        symbol.qualified_name
                    ),
                    "kind": symbol.kind,

                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,

                    "code": symbol.code,
                    "embedding": embedding,
                },
            }
        )

    if actions:
        succeeded, errors = bulk(
            elasticsearch_client.options(
                request_timeout=60
            ),
            actions,
        )

        if errors or succeeded != len(actions):
            raise RuntimeError("Incremental Elasticsearch bulk indexing was incomplete; retry synchronization.")

        refresh_symbol_index(index_name)

    return len(actions)


# ---------------------------------------------------------------------
# Elasticsearch result formatting
# ---------------------------------------------------------------------

def _complete_hits(response) -> list[dict]:
    if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
        raise IncompleteSearchError("Elasticsearch search did not complete.")
    payload = response.get("hits")
    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
        raise InvalidSearchResponseError("Invalid retrieval hit collection.")
    return payload["hits"]


def _validate_hit_source(source) -> None:
    if not isinstance(source, dict):
        raise InvalidSearchResponseError("Invalid retrieval document.")
    for field in ("repository", "path", "name", "qualified_name", "kind", "code"):
        if not isinstance(source.get(field), str):
            raise InvalidSearchResponseError("Invalid retrieval document field.")
    start, end = source.get("start_line"), source.get("end_line")
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise InvalidSearchResponseError("Invalid retrieval line range.")
    if source.get("language") is not None and not isinstance(source["language"], str):
        raise InvalidSearchResponseError("Invalid retrieval language.")
    if type(source.get("is_test", False)) is not bool:
        raise InvalidSearchResponseError("Invalid retrieval test flag.")


def _format_hits(
    hits: list[dict],
) -> list[dict]:
    """
    Convert raw Elasticsearch hits into CodeAtlas search results.
    """

    results = []

    if not isinstance(hits, list):
        raise InvalidSearchResponseError("Invalid retrieval hit collection.")
    for hit in hits:
        if not isinstance(hit, dict):
            raise InvalidSearchResponseError("Invalid retrieval hit.")
        source = hit.get("_source")
        score = hit.get("_score")
        if score is None:
            score = 0.0
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise InvalidSearchResponseError("Invalid retrieval score.")
        try:
            score = float(score)
        except OverflowError as exc:
            raise InvalidSearchResponseError("Retrieval score overflow.") from exc
        if not math.isfinite(score):
            raise InvalidSearchResponseError("Invalid retrieval score.")
        _validate_hit_source(source)

        results.append(
            {
                "score": float(score),

                "repository": (
                    source["repository"]
                ),

                "path": source["path"],

                "language": source.get(
                    "language"
                ),

                "is_test": source.get(
                    "is_test",
                    False,
                ),

                "name": source["name"],

                "qualified_name": source[
                    "qualified_name"
                ],

                "kind": source["kind"],

                "start_line": source[
                    "start_line"
                ],

                "end_line": source[
                    "end_line"
                ],

                "code": source["code"],
            }
        )

    return results


# ---------------------------------------------------------------------
# BM25 lexical retrieval
# ---------------------------------------------------------------------

def bm25_search(
    query: str,
    limit: int = 10,
    *,
    index_name: str | None = None,
) -> list[dict]:
    """
    Perform lexical code search using Elasticsearch BM25.

    Test code is excluded unless the query appears to request tests.
    The symbol index must already exist; indexing owns index creation.
    """

    lexical_query = {
        "multi_match": {
            "query": query,

            "fields": [
                "qualified_name^5",
                "name^4",
                "path^2",
                "code",
            ],

            "type": "best_fields",
        }
    }

    if query_has_test_intent(query):
        final_query = lexical_query

    else:
        final_query = {
            "bool": {
                "must": [
                    lexical_query
                ],

                "filter": [
                    {
                        "term": {
                            "is_test": False
                        }
                    }
                ],
            }
        }

    response = (
        _search_client()
        .search(
            index=index_name or resolve_search_index(),
            size=limit,
            query=final_query,
            allow_partial_search_results=False,
        )
    )

    return _format_hits(
        _complete_hits(response)
    )


# ---------------------------------------------------------------------
# Semantic vector retrieval
# ---------------------------------------------------------------------

def semantic_search(
    query: str,
    limit: int = 10,
    *,
    index_name: str | None = None,
) -> list[dict]:
    """
    Perform semantic code retrieval using vector kNN search.

    Test files are filtered for normal implementation-oriented queries.
    The symbol index must already exist; indexing owns index creation.
    """

    query_vector = embed_text(
        query
    )
    try:
        validate_embedding_vector(query_vector)
    except RuntimeError as exc:
        raise InvalidQueryEmbeddingError("Query embedding was invalid.") from exc

    knn_query = {
        "field": "embedding",

        "query_vector": (
            query_vector
        ),

        "k": limit,

        # Larger candidate pool improves recall
        # as the corpus grows.
        "num_candidates": max(
            limit * 8,
            100,
        ),
    }

    if not query_has_test_intent(query):
        knn_query["filter"] = {
            "term": {
                "is_test": False
            }
        }

    response = (
        _search_client()
        .search(
            index=index_name or resolve_search_index(),
            size=limit,
            knn=knn_query,
            allow_partial_search_results=False,
        )
    )

    return _format_hits(
        _complete_hits(response)
    )


# ---------------------------------------------------------------------
# Score normalization
# ---------------------------------------------------------------------

def _min_max_normalize(
    values: list[float],
) -> list[float]:
    """
    Normalize scores into the [0, 1] range.

    BM25 and vector similarity scores are not directly comparable,
    so normalization is required before weighted fusion.
    """

    if not values:
        return []

    if not all(math.isfinite(value) for value in values):
        raise InvalidSearchResponseError("Invalid retrieval scores.")

    minimum = min(values)
    maximum = max(values)

    if maximum == minimum:
        return [
            1.0
            for _ in values
        ]

    if not math.isfinite(maximum - minimum):
        raise InvalidSearchResponseError("Retrieval score range overflow.")

    return [
        (
            value - minimum
        )
        / (
            maximum - minimum
        )
        for value in values
    ]


# ---------------------------------------------------------------------
# Weighted hybrid retrieval
# ---------------------------------------------------------------------

def hybrid_search_weighted(
    query: str,
    limit: int = 10,
    semantic_weight: float = 0.60,
) -> list[dict]:
    """
    Run BM25 and semantic retrieval concurrently, then perform
    normalized weighted score fusion.
    """

    if not 0.0 <= semantic_weight <= 1.0:
        raise ValueError(
            "semantic_weight must be between 0 and 1."
        )

    candidate_limit = max(
        limit * 4,
        20,
    )

    # ---------------------------------------------------------------
    # BM25 and semantic retrieval are independent.
    # Run both concurrently instead of sequentially.
    # ---------------------------------------------------------------

    index_name = resolve_search_index()

    with ThreadPoolExecutor(
        max_workers=2
    ) as executor:
        bm25_future = executor.submit(
            bm25_search,
            query,
            candidate_limit,
            index_name=index_name,
        )

        semantic_future = executor.submit(
            semantic_search,
            query,
            candidate_limit,
            index_name=index_name,
        )

        bm25_results = bm25_future.result()
        semantic_results = (
            semantic_future.result()
        )

    # ---------------------------------------------------------------
    # Normalize scores.
    # ---------------------------------------------------------------

    bm25_scores = _min_max_normalize(
        [
            float(result["score"] or 0.0)
            for result in bm25_results
        ]
    )

    semantic_scores = _min_max_normalize(
        [
            float(result["score"] or 0.0)
            for result in semantic_results
        ]
    )

    candidates: dict[
        tuple[str, str, int],
        dict,
    ] = {}

    # ---------------------------------------------------------------
    # Add BM25 candidates.
    # ---------------------------------------------------------------

    for rank, (
        result,
        normalized_score,
    ) in enumerate(
        zip(
            bm25_results,
            bm25_scores,
        ),
        start=1,
    ):
        key = (
            result["repository"],
            result["path"],
            result["start_line"],
        )

        candidates[key] = {
            **result,
            "bm25_score": normalized_score,
            "semantic_score": 0.0,
            "bm25_rank": rank,
            "semantic_rank": None,
        }

    # ---------------------------------------------------------------
    # Merge semantic candidates.
    # ---------------------------------------------------------------

    for rank, (
        result,
        normalized_score,
    ) in enumerate(
        zip(
            semantic_results,
            semantic_scores,
        ),
        start=1,
    ):
        key = (
            result["repository"],
            result["path"],
            result["start_line"],
        )

        if key not in candidates:
            candidates[key] = {
                **result,
                "bm25_score": 0.0,
                "semantic_score": (
                    normalized_score
                ),
                "bm25_rank": None,
                "semantic_rank": rank,
            }

        else:
            candidates[key][
                "semantic_score"
            ] = normalized_score

            candidates[key][
                "semantic_rank"
            ] = rank

    # ---------------------------------------------------------------
    # Weighted fusion.
    # ---------------------------------------------------------------

    bm25_weight = (
        1.0 - semantic_weight
    )

    for candidate in candidates.values():
        candidate["score"] = round(
            (
                semantic_weight
                * candidate[
                    "semantic_score"
                ]
            )
            +
            (
                bm25_weight
                * candidate[
                    "bm25_score"
                ]
            ),
            8,
        )

    ranked_results = sorted(
        candidates.values(),
        key=lambda result: result[
            "score"
        ],
        reverse=True,
    )

    return ranked_results[:limit]
# ---------------------------------------------------------------------
# Default CodeAtlas hybrid search
# ---------------------------------------------------------------------

def hybrid_search(
    query: str,
    limit: int = 10,
) -> list[dict]:
    """
    Run CodeAtlas's default hybrid retrieval strategy.

    Current tuned configuration:

        Semantic: 60%
        BM25:     40%

    Value comes from application configuration.
    """

    return hybrid_search_weighted(
        query=query,
        limit=limit,
        semantic_weight=(
            settings.hybrid_semantic_weight
        ),
    )


# ---------------------------------------------------------------------
# Public search API
# ---------------------------------------------------------------------

def search_code(
    query: str,
    limit: int = 10,
) -> list[dict]:
    return hybrid_search(
        query=query,
        limit=limit,
    )
