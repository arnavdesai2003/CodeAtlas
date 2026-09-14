from functools import lru_cache

from sentence_transformers import SentenceTransformer


MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMS = 384


@lru_cache(maxsize=1)
def get_embedding_model() -> SentenceTransformer:
    return SentenceTransformer(MODEL_NAME)


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