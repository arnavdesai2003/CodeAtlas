from app.search.engine import (
    bm25_search,
    semantic_search,
    hybrid_search,
)
from scripts.evaluate_multirepo import TEST_CASES


SEARCH_LIMIT = 50


def find_rank(results, case):
    for rank, result in enumerate(results, start=1):
        if (
            result["repository"] == case["repository"]
            and result["qualified_name"] == case["expected"]
        ):
            return rank

    return None


def display_top(results, count=5):
    for rank, result in enumerate(
        results[:count],
        start=1,
    ):
        print(
            f"    {rank}. "
            f"[{result['repository']}] "
            f"{result['qualified_name']} "
            f"score={result['score']}"
        )


def main():
    misses = 0

    print()
    print("=" * 100)
    print("CODEATLAS RETRIEVAL FAILURE ANALYSIS")
    print("=" * 100)

    for number, case in enumerate(
        TEST_CASES,
        start=1,
    ):
        bm25 = bm25_search(
            case["query"],
            limit=SEARCH_LIMIT,
        )

        semantic = semantic_search(
            case["query"],
            limit=SEARCH_LIMIT,
        )

        hybrid = hybrid_search(
            case["query"],
            limit=SEARCH_LIMIT,
        )

        bm25_rank = find_rank(
            bm25,
            case,
        )

        semantic_rank = find_rank(
            semantic,
            case,
        )

        hybrid_rank = find_rank(
            hybrid,
            case,
        )

        if (
            hybrid_rank is not None
            and hybrid_rank <= 10
        ):
            continue

        misses += 1

        print()
        print("-" * 100)

        print(
            f"{number}. [{case['repository']}] "
            f"{case['query']}"
        )

        print(
            f"Expected: {case['expected']}"
        )

        print(
            f"BM25 rank     : {bm25_rank}"
        )

        print(
            f"Semantic rank : {semantic_rank}"
        )

        print(
            f"Hybrid rank   : {hybrid_rank}"
        )

        print()
        print("Hybrid top 5:")

        display_top(
            hybrid,
            count=5,
        )

    print()
    print("=" * 100)
    print(
        f"Hybrid Recall@10 misses: "
        f"{misses}/{len(TEST_CASES)}"
    )


if __name__ == "__main__":
    main()