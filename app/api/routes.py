from ipaddress import ip_address
from elasticsearch import ApiError
from elastic_transport import TransportError
from app.search.errors import IncompleteSearchError

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from app.core.clients import elasticsearch_client, redis_client
from app.core.config import settings
from app.db.database import engine
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.search.service import search_with_cache
from app.api.schemas import (
    RepositoryCreateRequest,
    RepositoryCreateResponse,
)
from app.db.database import engine, get_db
from app.indexer.repository import ingest_repository
from app.indexer.errors import (
    InvalidRepositoryURL, RepositoryConflict, RepositoryCloneFailed,
    RepositoryNotFound, RepositoryCloneMissing, UnsafeClonePath,
)
from app.db.models import Repository
from app.indexer.incremental import sync_repository, RepositorySyncInProgress
from app.api.auth import require_api_key


router = APIRouter(dependencies=[Depends(require_api_key)])


class SearchRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Code search query",
    )

    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of search results",
    )

    @field_validator("query")
    @classmethod
    def require_query_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Query must contain non-whitespace text.")
        return value


@router.get("/health")
def health_check():
    services = {}

    # PostgreSQL
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        services["postgresql"] = {
            "status": "healthy"
        }

    except Exception as exc:
        services["postgresql"] = {
            "status": "unhealthy",
            "error": type(exc).__name__,
        }

    # Elasticsearch
    try:
        if elasticsearch_client.ping():
            services["elasticsearch"] = {
                "status": "healthy"
            }
        else:
            services["elasticsearch"] = {
                "status": "unhealthy"
            }

    except Exception as exc:
        services["elasticsearch"] = {
            "status": "unhealthy",
            "error": type(exc).__name__,
        }

    # Redis
    try:
        redis_client.ping()

        services["redis"] = {
            "status": "healthy"
        }

    except Exception as exc:
        services["redis"] = {
            "status": "unhealthy",
            "error": type(exc).__name__,
        }

    all_healthy = all(
        service["status"] == "healthy"
        for service in services.values()
    )

    response = {
        "status": "healthy" if all_healthy else "unhealthy",
        "service": "codeatlas-api",
        "dependencies": services,
    }

    return JSONResponse(
        status_code=200 if all_healthy else 503,
        content=response,
    )
    
@router.post(
    "/repositories",
    response_model=RepositoryCreateResponse,
    status_code=201,
)
def add_repository(
    request: RepositoryCreateRequest,
    db: Session = Depends(get_db),
):
    try:
        return ingest_repository(
            db=db,
            clone_url=request.clone_url,
        )

    except InvalidRepositoryURL as exc:
        raise HTTPException(status_code=400, detail="Invalid GitHub repository URL.") from exc
    except RepositoryConflict as exc:
        raise HTTPException(status_code=409, detail="Repository already registered or clone directory occupied.") from exc
    except RepositoryCloneFailed as exc:
        raise HTTPException(status_code=502, detail="Repository clone failed.") from exc
    except UnsafeClonePath as exc:
        raise HTTPException(status_code=409, detail="Repository directory is unsafe; inspect local state.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Repository creation failed; inspect state before retrying.") from exc

@router.get("/repositories")
def list_repositories(
    db: Session = Depends(get_db),
):
    repositories = (
        db.query(Repository)
        .order_by(Repository.id)
        .all()
    )

    return {
        "count": len(repositories),
        "repositories": [
            {
                "id": repository.id,
                "name": repository.name,
                "clone_url": repository.clone_url,
                "default_branch": repository.default_branch,
                "last_indexed_commit": (
                    repository.last_indexed_commit
                ),
            }
            for repository in repositories
        ],
    }


@router.post("/search")
def search_code(
    request: SearchRequest,
    http_request: Request,
    response: Response,
    benchmark_bypass: bool = Header(default=False, alias="X-CodeAtlas-Benchmark-Bypass"),
):
    if benchmark_bypass:
        try:
            loopback = bool(
                http_request.client and ip_address(http_request.client.host).is_loopback
            )
        except ValueError:
            loopback = False
        if not (
            settings.benchmark_cache_bypass_enabled
            and settings.app_env in {"development", "test"}
            and loopback
        ):
            raise HTTPException(status_code=403, detail="Benchmark cache bypass is disabled.")
        response.headers["X-CodeAtlas-Cache-Bypassed"] = "true"

    try:
        search_result = search_with_cache(
            query=request.query,
            limit=request.limit,
            bypass_cache=benchmark_bypass,
        )
    except (ApiError, TransportError, IncompleteSearchError) as exc:
        # Never expose backend URLs, index names or failure bodies to callers.
        raise HTTPException(status_code=503, detail="Search backend unavailable.") from exc

    return {
        "query": request.query,
        "limit": request.limit,
        "count": len(
            search_result["results"]
        ),
        "cache_hit": search_result[
            "cache_hit"
        ],
        "cache_coalesced": search_result.get("cache_coalesced", False),
        "coalescing_wait_ms": search_result.get("coalescing_wait_ms", 0.0),
        "search_latency_ms": search_result[
            "search_latency_ms"
        ],
        "elasticsearch_latency_ms": (
            search_result.get(
                "elasticsearch_latency_ms"
            )
        ),
        "results": search_result["results"],
    }
    
@router.post(
    "/repositories/{repository_id}/sync"
)
def synchronize_repository(
    repository_id: int,
    db: Session = Depends(get_db),
):
    try:
        return sync_repository(
            db=db,
            repository_id=repository_id,
        )

    except RepositorySyncInProgress as exc:
        raise HTTPException(status_code=409, detail="Repository writer is busy or pending publication blocks synchronization.") from exc

    except UnsafeClonePath as exc:
        raise HTTPException(status_code=409, detail="Repository directory is unsafe; inspect local state.") from exc

    except RepositoryNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="Repository not found.",
        ) from exc

    except RepositoryCloneMissing as exc:
        raise HTTPException(
            status_code=409,
            detail="Repository clone is missing; inspect local state.",
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Repository synchronization failed; retry to resume pending work.",
        ) from exc
