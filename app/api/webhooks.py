import hashlib
import hmac
import json
import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    HTTPException,
    Request,
)

from app.core.config import settings
from app.db.database import SessionLocal
from app.db.models import Repository
from app.indexer.incremental import sync_repository


router = APIRouter(
    prefix="/webhooks",
    tags=["webhooks"],
)
logger = logging.getLogger(__name__)


def _unique_payload_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate webhook JSON field")
        result[key] = value
    return result


def _reject_nonfinite_constant(value):
    raise ValueError("Nonstandard webhook JSON constant")


def verify_github_signature(
    payload: bytes,
    signature: str | None,
) -> None:
    secret = settings.github_webhook_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=503, detail="GitHub webhook is not configured.")
    if not signature:
        raise HTTPException(
            status_code=401,
            detail="Missing GitHub signature.",
        )

    expected_signature = (
        "sha256="
        + hmac.new(
            secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
    )

    if not signature.isascii() or not hmac.compare_digest(
        expected_signature,
        signature,
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid GitHub signature.",
        )


def sync_repository_background(
    repository_id: int,
) -> None:
    db = None

    try:
        db = SessionLocal()
        sync_repository(
            db=db,
            repository_id=repository_id,
        )

        logger.info("GitHub webhook sync completed repository_id=%s", repository_id)

    except Exception as exc:
        logger.error("GitHub webhook sync failed repository_id=%s error_type=%s",
                     repository_id, type(exc).__name__)

    finally:
        if db is not None:
            try:
                db.close()
            except Exception as exc:
                logger.error("GitHub webhook session close failed repository_id=%s error_type=%s",
                             repository_id, type(exc).__name__)


@router.post(
    "/github",
    status_code=202,
)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    payload_bytes = await request.body()

    signature = request.headers.get(
        "X-Hub-Signature-256"
    )

    verify_github_signature(
        payload=payload_bytes,
        signature=signature,
    )

    event = request.headers.get(
        "X-GitHub-Event"
    )

    try:
        payload = json.loads(payload_bytes, object_pairs_hook=_unique_payload_object,
                             parse_constant=_reject_nonfinite_constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be an object.")

    # GitHub sends this when the webhook
    # is first configured.
    if event == "ping":
        return {
            "status": "ok",
            "event": "ping",
        }

    if event != "push":
        return {
            "status": "ignored",
            "event": event,
        }

    repository_data = payload.get(
        "repository",
        {}
    )

    if not isinstance(repository_data, dict):
        raise HTTPException(status_code=400, detail="Webhook repository must be an object.")

    clone_url = repository_data.get(
        "clone_url"
    )

    if not isinstance(clone_url, str) or not clone_url.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Webhook payload does not "
                "contain repository.clone_url."
            ),
        )

    db = SessionLocal()

    try:
        repository = (
            db.query(Repository)
            .filter(
                Repository.clone_url
                == clone_url
            )
            .first()
        )

        if repository is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "Repository is not registered "
                    "in CodeAtlas."
                ),
            )

        repository_id = repository.id

    finally:
        db.close()

    background_tasks.add_task(
        sync_repository_background,
        repository_id,
    )

    return {
        "status": "accepted",
        "event": "push",
        "repository_id": repository_id,
    }
