import argparse

from app.search.engine import hybrid_search_weighted
from scripts.evaluate_search import TEST_CASES
from scripts.evaluate_multirepo import find_rank, validate_results, validate_test_cases


WEIGHTS = [
    0.50,
    0.55,
    0.60,
    0.65,
    0.70,
    0.75,
    0.80,
    0.85,
    0.90,
    0.95,
    1.00,
]


def evaluate_weight(
    semantic_weight: float,
) -> dict:
    if not TEST_CASES:
        raise ValueError("No evaluation cases")
    ranks = []

    for case in TEST_CASES:
        results = hybrid_search_weighted(
            query=case["query"],
            limit=10,
            semantic_weight=semantic_weight,
        )

        validate_results(results)
        rank = find_rank(results, case)

        ranks.append(rank)

    total = len(ranks)

    recall_1 = sum(
        rank is not None and rank <= 1
        for rank in ranks
    ) / total

    recall_3 = sum(
        rank is not None and rank <= 3
        for rank in ranks
    ) / total

    recall_5 = sum(
        rank is not None and rank <= 5
        for rank in ranks
    ) / total

    recall_10 = sum(
        rank is not None and rank <= 10
        for rank in ranks
    ) / total

    mrr = sum(
        1 / rank if rank else 0
        for rank in ranks
    ) / total

    return {
        "weight": semantic_weight,
        "recall_1": recall_1,
        "recall_3": recall_3,
        "recall_5": recall_5,
        "recall_10": recall_10,
        "mrr": mrr,
    }


def main(argv=None):
    argparse.ArgumentParser(description="Compare weights on validated micrograd cases; does not change settings.").parse_args(argv)
    valid, invalid = validate_test_cases(TEST_CASES)
    if invalid or not valid:
        print("Tuning incomplete: ground truth invalid; retrieval skipped.")
        return 1
    results = [
        evaluate_weight(weight)
        for weight in WEIGHTS
    ]

    print()
    print("HYBRID WEIGHT TUNING")
    print("=" * 78)

    print(
        f"{'Semantic':<12}"
        f"{'BM25':<12}"
        f"{'R@1':<10}"
        f"{'R@3':<10}"
        f"{'R@5':<10}"
        f"{'R@10':<10}"
        f"{'MRR':<10}"
    )

    for result in results:
        print(
            f"{result['weight']:<12.2f}"
            f"{1-result['weight']:<12.2f}"
            f"{result['recall_1']:<10.3f}"
            f"{result['recall_3']:<10.3f}"
            f"{result['recall_5']:<10.3f}"
            f"{result['recall_10']:<10.3f}"
            f"{result['mrr']:<10.3f}"
        )

    valid = [
        result
        for result in results
        if result["recall_10"] == 1.0
    ]

    if valid:
        best = max(
            valid,
            key=lambda result: (
                result["mrr"],
                result["recall_1"],
                result["recall_3"],
            ),
        )
    else:
        best = max(
            results,
            key=lambda result: (
                result["recall_10"],
                result["mrr"],
            ),
        )

    print()
    print("BEST CONFIGURATION")
    print("=" * 78)

    print(
        f"Semantic weight : {best['weight']:.2f}"
    )

    print(
        f"BM25 weight     : {1-best['weight']:.2f}"
    )

    print(
        f"Recall@1        : {best['recall_1']:.3f}"
    )

    print(
        f"Recall@3        : {best['recall_3']:.3f}"
    )

    print(
        f"Recall@5        : {best['recall_5']:.3f}"
    )

    print(
        f"Recall@10       : {best['recall_10']:.3f}"
    )

    print(
        f"MRR             : {best['mrr']:.3f}"
    )
    return 0


def cli(argv=None):
    try:
        return main(argv)
    except Exception as exc:
        print(f"{type(exc).__name__}: Tuning failed; partial output is not a complete result.")
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
