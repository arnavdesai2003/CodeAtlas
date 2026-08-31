from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from sqlalchemy.orm import Session

from app.core.clients import elasticsearch_client
from app.db.models import CodeFile, CodeSymbol, Repository


INDEX_NAME = "codeatlas_symbols"


def create_symbol_index(
    client: Elasticsearch = elasticsearch_client,
) -> None:
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
            }
        },
    )


def index_repository_in_elasticsearch(
    db: Session,
    repository_id: int,
) -> dict:
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
        db.query(CodeSymbol, CodeFile)
        .join(
            CodeFile,
            CodeSymbol.file_id == CodeFile.id,
        )
        .filter(
            CodeSymbol.repository_id == repository_id
        )
        .all()
    )

    actions = []

    for symbol, code_file in rows:
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
                    "symbol_id": symbol.id,
                    "name": symbol.name,
                    "qualified_name": symbol.qualified_name,
                    "kind": symbol.kind,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "code": symbol.code,
                },
            }
        )

    if actions:
        bulk(
        elasticsearch_client.options(
            request_timeout=30
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


def search_code(
    query: str,
    limit: int = 10,
) -> list[dict]:
    create_symbol_index()

    response = elasticsearch_client.search(
        index=INDEX_NAME,
        size=limit,
        query={
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
        },
    )

    results = []

    for hit in response["hits"]["hits"]:
        source = hit["_source"]

        results.append(
            {
                "score": hit["_score"],
                "repository": source["repository"],
                "path": source["path"],
                "language": source["language"],
                "name": source["name"],
                "qualified_name": source[
                    "qualified_name"
                ],
                "kind": source["kind"],
                "start_line": source["start_line"],
                "end_line": source["end_line"],
                "code": source["code"],
            }
        )

    return results

def delete_paths_from_elasticsearch(
    repository_id: int,
    paths: list[str],
) -> None:
    if not paths:
        return

    create_symbol_index()

    elasticsearch_client.delete_by_query(
        index=INDEX_NAME,
        query={
            "bool": {
                "filter": [
                    {
                        "term": {
                            "repository_id": repository_id
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


def index_files_in_elasticsearch(
    db: Session,
    file_ids: list[int],
) -> int:
    if not file_ids:
        return 0

    create_symbol_index()

    rows = (
        db.query(CodeSymbol, CodeFile, Repository)
        .join(
            CodeFile,
            CodeSymbol.file_id == CodeFile.id,
        )
        .join(
            Repository,
            CodeSymbol.repository_id == Repository.id,
        )
        .filter(
            CodeFile.id.in_(file_ids)
        )
        .all()
    )

    actions = []

    for symbol, code_file, repository in rows:
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
                    "symbol_id": symbol.id,
                    "name": symbol.name,
                    "qualified_name": symbol.qualified_name,
                    "kind": symbol.kind,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "code": symbol.code,
                },
            }
        )

    if actions:
        bulk(
            elasticsearch_client.options(
                request_timeout=30
            ),
            actions,
        )

        elasticsearch_client.indices.refresh(
            index=INDEX_NAME
        )

    return len(actions)