from app.search.engine import (
    bm25_search,
    semantic_search,
    hybrid_search,
)


QUERY = "function that computes gradients during backpropagation"


def print_results(
    title: str,
    results: list[dict],
):
    print()
    print(title)
    print("=" * 70)

    for rank, result in enumerate(
        results,
        start=1,
    ):
        print(
            f"{rank}. "
            f"{result['qualified_name']} "
            f"({result['path']})"
        )

        print(
            f"   score: {result['score']}"
        )

        if "bm25_rank" in result:
            print(
                f"   bm25_rank: "
                f"{result['bm25_rank']}"
            )

        if "semantic_rank" in result:
            print(
                f"   semantic_rank: "
                f"{result['semantic_rank']}"
            )


def main():
    print(f'Query: "{QUERY}"')

    print("Running BM25...")
    bm25_results = bm25_search(
        QUERY,
        limit=5,
    )
    print_results(
        "BM25",
        bm25_results,
    )

    print("Running semantic search...")
    semantic_results = semantic_search(
        QUERY,
        limit=5,
    )
    print_results(
        "SEMANTIC",
        semantic_results,
    )

    print("Running hybrid search...")
    hybrid_results = hybrid_search(
        QUERY,
        limit=5,
    )
    print_results(
        "HYBRID",
        hybrid_results,
    )


if __name__ == "__main__":
    main()