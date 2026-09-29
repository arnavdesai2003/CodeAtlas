from functools import lru_cache
from threading import Lock

from sentence_transformers import SentenceTransformer
import torch

from app.core.config import settings


MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMS = 384
_model_init_lock = Lock()


@lru_cache(maxsize=1)
def _load_embedding_model() -> SentenceTransformer:
    # Optional process-wide setting: apply before model inference begins.
    if settings.torch_num_threads is not None:
        torch.set_num_threads(settings.torch_num_threads)
    if settings.embedding_device is None:
        return SentenceTransformer(MODEL_NAME)
    return SentenceTransformer(MODEL_NAME, device=settings.embedding_device)


def get_embedding_model() -> SentenceTransformer:
    # lru_cache alone can initialize multiple models on concurrent cold misses.
    # Serialize initialization, not inference, including the global thread setup.
    with _model_init_lock:
        return _load_embedding_model()


def embed_text(text: str) -> list[float]:
    model = get_embedding_model()

    vector = model.encode(
        text,
        normalize_embeddings=True,
        show_progress_bar=False,
    )

    return vector.tolist()


def build_symbol_embedding_text(
    repository: str,
    path: str,
    language: str | None,
    qualified_name: str,
    kind: str,
    code: str,
) -> str:
    code_lines = code.splitlines()

    signature = (
        code_lines[0].strip()
        if code_lines
        else ""
    )

    code_preview = "\n".join(
        code_lines[:40]
    )

    return "\n".join(
        [
            f"Symbol: {qualified_name}",
            f"Type: {kind}",
            f"Signature: {signature}",
            f"Repository: {repository}",
            f"File: {path}",
            f"Language: {language or 'unknown'}",
            "",
            "Implementation:",
            code_preview,
        ]
    )   
def embed_texts(
    texts: list[str],
    batch_size: int = 32,
) -> list[list[float]]:
    if not texts:
        return []

    model = get_embedding_model()

    vectors = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
    )

    return vectors.tolist()
