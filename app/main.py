from contextlib import asynccontextmanager
from app.api.webhooks import router as webhook_router
from fastapi import FastAPI

from app.api.routes import router
from app.db.database import init_db
from app.api.body_limit import RequestBodyLimit
from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    yield


app = FastAPI(
    title="CodeAtlas API",
    description=(
        "Distributed code search engine for indexing "
        "and searching source code."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


app.add_middleware(RequestBodyLimit, max_bytes=settings.request_body_max_bytes)
app.include_router(router)
app.include_router(webhook_router)


@app.get("/")
def root():
    return {
        "name": "CodeAtlas",
        "status": "running",
        "version": "0.1.0",
    }
