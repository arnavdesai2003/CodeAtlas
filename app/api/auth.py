"""Single-key access boundary for search and repository APIs."""
import hmac

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from app.core.config import settings


api_key_header = APIKeyHeader(
    name="X-CodeAtlas-API-Key", scheme_name="CodeAtlasAPIKey", auto_error=False,
    description="Required when API_KEY is configured. Only development/test may run without a configured key.",
)


def require_api_key(api_key: str | None = Security(api_key_header)):
    expected = settings.api_key.get_secret_value()
    if not expected:
        if settings.app_env in {"development", "test"}:
            return
        raise HTTPException(status_code=503, detail="API authentication is not configured.")
    if api_key is None or not hmac.compare_digest(api_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")
