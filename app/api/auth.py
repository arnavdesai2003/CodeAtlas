"""Single-key access boundary for search and repository APIs."""
import hmac

from fastapi import Header, HTTPException, Request
from app.core.config import settings


def require_api_key(request: Request,
                    api_key: str | None = Header(default=None, alias="X-CodeAtlas-API-Key")):
    # Liveness/dependency inspection remains available to local operators.
    if request.url.path == "/health":
        return
    expected = settings.api_key.get_secret_value()
    if not expected:
        if settings.app_env in {"development", "test"}:
            return
        raise HTTPException(status_code=503, detail="API authentication is not configured.")
    if api_key is None or not hmac.compare_digest(api_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
