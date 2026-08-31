from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.search.engine import search_code as elasticsearch_search
from app.core.clients import elasticsearch_client, redis_client
from app.db.database import engine
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import (
    RepositoryCreateRequest,
    RepositoryCreateResponse,
)
from app.db.database import engine, get_db
from app.indexer.repository import ingest_repository
from app.db.models import Repository


router = APIRouter()


class SearchRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        description="Code search query",
    )

    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of search results",
    )


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

    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

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
def search_code(request: SearchRequest):
    results = elasticsearch_search(
        query=request.query,
        limit=request.limit,
    )

    return {
        "query": request.query,
        "limit": request.limit,
        "count": len(results),
        "results": results,
    }