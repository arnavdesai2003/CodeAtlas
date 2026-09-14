from functools import lru_cache

from sentence_transformers import CrossEncoder


RERANKER_MODEL = (
    "cross-encoder/ms-marco-MiniLM-L-6-v2"
)


@lru_cache(maxsize=1)
def get_reranker() -> CrossEncoder:
    return CrossEncoder(
        RERANKER_MODEL
    )


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

    reranked = []

    for result, score in zip(
        results,
        scores,
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