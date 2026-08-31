import hashlib
import hmac

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


def verify_github_signature(
    payload: bytes,
    signature: str | None,
) -> None:
    if not signature:
        raise HTTPException(
            status_code=401,
            detail="Missing GitHub signature.",
        )

    expected_signature = (
        "sha256="
        + hmac.new(
            settings.github_webhook_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
    )

    if not hmac.compare_digest(
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
    db = SessionLocal()

    try:
        result = sync_repository(
            db=db,
            repository_id=repository_id,
        )

        print(
            "GitHub webhook sync completed:",
            result,
        )

    except Exception as exc:
        print(
            "GitHub webhook sync failed:",
            type(exc).__name__,
            str(exc),
        )

    finally:
        db.close()


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

    payload = await request.json()

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

    clone_url = repository_data.get(
        "clone_url"
    )

    if not clone_url:
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