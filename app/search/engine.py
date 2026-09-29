from __future__ import annotations

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from sqlalchemy.orm import Session
from concurrent.futures import ThreadPoolExecutor
from app.core.clients import elasticsearch_client
from app.core.config import settings
from app.db.models import CodeFile, CodeSymbol, Repository, RepositorySyncJob
from app.search.embeddings import (
    EMBEDDING_DIMS,
    build_symbol_embedding_text,
    embed_text,
    embed_texts,
)
from app.search.reranker import rerank_results

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

INDEX_NAME = "codeatlas_symbols"


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
) -> None:
    """
    Create the CodeAtlas Elasticsearch index if it does not exist.
    """

    if client.indices.exists(index=INDEX_NAME):
        return

    client.indices.create(
        index=INDEX_NAME,
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


# ---------------------------------------------------------------------
# Full repository indexing
# ---------------------------------------------------------------------

def index_repository_in_elasticsearch(
    db: Session,
    repository_id: int,
) -> dict:
    """
    Index all parsed symbols from one repository.

    Semantic embeddings are generated in batches.
    """

    if db.get(RepositorySyncJob, repository_id) is not None:
        raise RuntimeError("Finish pending repository synchronization before full Elasticsearch indexing.")

    create_symbol_index()

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
            "index": INDEX_NAME,
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

    if len(embeddings) != len(rows):
        raise RuntimeError(
            "Embedding count does not match symbol count."
        )

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
                "_index": INDEX_NAME,
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
        bulk(
            elasticsearch_client.options(
                request_timeout=60
            ),
            actions,
        )

        elasticsearch_client.indices.refresh(
            index=INDEX_NAME
        )

    return {
        "repository_id": repository.id,
        "repository": repository.name,
        "symbols_indexed": len(actions),
        "index": INDEX_NAME,
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

    create_symbol_index()

    response = elasticsearch_client.options(
        request_timeout=60
    ).delete_by_query(
        index=INDEX_NAME,
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
    if response.get("timed_out") or response.get("failures") or response.get("version_conflicts"):
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

    create_symbol_index()

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

    if len(embeddings) != len(rows):
        raise RuntimeError(
            "Embedding count does not match symbol count."
        )

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
                "_index": INDEX_NAME,
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
        bulk(
            elasticsearch_client.options(
                request_timeout=60
            ),
            actions,
        )

        elasticsearch_client.indices.refresh(
            index=INDEX_NAME
        )

    return len(actions)


# ---------------------------------------------------------------------
# Elasticsearch result formatting
# ---------------------------------------------------------------------

def _format_hits(
    hits: list[dict],
) -> list[dict]:
    """
    Convert raw Elasticsearch hits into CodeAtlas search results.
    """

    results = []

    for hit in hits:
        source = hit["_source"]

        results.append(
            {
                "score": float(
                    hit.get("_score") or 0.0
                ),

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
        elasticsearch_client
        .options(
            request_timeout=60
        )
        .search(
            index=INDEX_NAME,
            size=limit,
            query=final_query,
        )
    )

    return _format_hits(
        response["hits"]["hits"]
    )


# ---------------------------------------------------------------------
# Semantic vector retrieval
# ---------------------------------------------------------------------

def semantic_search(
    query: str,
    limit: int = 10,
) -> list[dict]:
    """
    Perform semantic code retrieval using vector kNN search.

    Test files are filtered for normal implementation-oriented queries.
    The symbol index must already exist; indexing owns index creation.
    """

    query_vector = embed_text(
        query
    )

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
        elasticsearch_client
        .options(
            request_timeout=60
        )
        .search(
            index=INDEX_NAME,
            size=limit,
            knn=knn_query,
        )
    )

    return _format_hits(
        response["hits"]["hits"]
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

    minimum = min(values)
    maximum = max(values)

    if maximum == minimum:
        return [
            1.0
            for _ in values
        ]

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

    with ThreadPoolExecutor(
        max_workers=2
    ) as executor:
        bm25_future = executor.submit(
            bm25_search,
            query,
            candidate_limit,
        )

        semantic_future = executor.submit(
            semantic_search,
            query,
            candidate_limit,
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
