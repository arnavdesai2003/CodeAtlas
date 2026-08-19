from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.clients import elasticsearch_client, redis_client
from app.db.database import engine


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


@router.post("/search")
def search_code(request: SearchRequest):
    return {
        "query": request.query,
        "limit": request.limit,
        "results": [],
    }