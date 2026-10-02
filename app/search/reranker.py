from functools import lru_cache
import math
from numbers import Real
from threading import Lock

from sentence_transformers import CrossEncoder
from app.search.errors import InvalidRerankerOutputError


RERANKER_MODEL = (
    "cross-encoder/ms-marco-MiniLM-L-6-v2"
)
_model_init_lock = Lock()


@lru_cache(maxsize=1)
def _load_reranker() -> CrossEncoder:
    return CrossEncoder(
        RERANKER_MODEL
    )


def get_reranker() -> CrossEncoder:
    # lru_cache permits duplicate construction on concurrent cold misses.
    # The lock covers initialization only; prediction remains concurrent.
    with _model_init_lock:
        return _load_reranker()


def build_rerank_document(
    result: dict,
) -> str:
    code = result.get(
        "code",
        "",
    )

    # Avoid passing extremely large methods/classes.
    code_preview = "\n".join(
        code.splitlines()[:40]
    )

    return "\n".join(
        [
            f"Repository: {result['repository']}",
            f"File: {result['path']}",
            f"Symbol: {result['qualified_name']}",
            f"Type: {result['kind']}",
            "",
            code_preview,
        ]
    )


def rerank_results(
    query: str,
    results: list[dict],
    limit: int,
) -> list[dict]:
    if not results:
        return []

    model = get_reranker()

    pairs = [
        [
            query,
            build_rerank_document(result),
        ]
        for result in results
    ]

    scores = model.predict(
        pairs,
        show_progress_bar=False,
    )

    try:
        if len(scores) != len(results):
            raise InvalidRerankerOutputError("Reranker score count does not match candidates.")
        checked_scores = []
        for score in scores:
            if isinstance(score, bool) or not isinstance(score, Real):
                raise InvalidRerankerOutputError("Reranker score was invalid.")
            value = float(score)
            if not math.isfinite(value):
                raise InvalidRerankerOutputError("Reranker score was invalid.")
            checked_scores.append(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidRerankerOutputError("Reranker output was invalid.") from exc

    reranked = []

    for result, score in zip(
        results,
        checked_scores,
    ):
        reranked.append(
            {
                **result,
                "reranker_score": float(score),
            }
        )

    reranked.sort(
        key=lambda result: result[
            "reranker_score"
        ],
        reverse=True,
    )

    return reranked[:limit]
