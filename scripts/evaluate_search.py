from app.search.engine import (
    bm25_search,
    semantic_search,
    hybrid_search,
)
from scripts.evaluate_multirepo import find_rank


TEST_CASES = [
    {
        "query": "function that computes gradients during backpropagation",
        "expected": "Value.backward",
    },
    {
        "query": "zero all gradients",
        "expected": "Module.zero_grad",
    },
    {
        "query": "relu activation function",
        "expected": "Value.relu",
    },
    {
        "query": "neural network layer",
        "expected": "Layer",
    },
    {
        "query": "multi layer perceptron",
        "expected": "MLP",
    },
    {
        "query": "call a neuron on input values",
        "expected": "Neuron.__call__",
    },
    {
        "query": "return trainable parameters",
        "expected": "Module.parameters",
    },
    {
        "query": "perform sanity check on autograd",
        "expected": "test_sanity_check",
    },
]

# These legacy cases describe micrograd, even when searching a larger corpus.
TEST_CASES = [{**case, "repository": "micrograd"} for case in TEST_CASES]


def evaluate(search_function, name: str):
    ranks = []

    print()
    print(name)
    print("=" * 70)

    for case in TEST_CASES:
        results = search_function(
            query=case["query"],
            limit=10,
        )

        rank = find_rank(results, case)

        ranks.append(rank)

        print(
            f"{case['query']:<48} "
            f"expected={case['expected']:<22} "
            f"rank={rank}"
        )

    total = len(TEST_CASES)

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

    reciprocal_ranks = [
        1 / rank if rank else 0
        for rank in ranks
    ]

    mrr = (
        sum(reciprocal_ranks)
        / total
    )

    print()
    print(f"Recall@1  : {recall_1:.3f}")
    print(f"Recall@3  : {recall_3:.3f}")
    print(f"Recall@5  : {recall_5:.3f}")
    print(f"Recall@10 : {recall_10:.3f}")
    print(f"MRR       : {mrr:.3f}")

    return {
        "Recall@1": recall_1,
        "Recall@3": recall_3,
        "Recall@5": recall_5,
        "Recall@10": recall_10,
        "MRR": mrr,
    }


def main():
    bm25_metrics = evaluate(
        bm25_search,
        "BM25",
    )

    semantic_metrics = evaluate(
        semantic_search,
        "SEMANTIC",
    )

    hybrid_metrics = evaluate(
        hybrid_search,
        "HYBRID",
    )

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    print(
        f"{'Metric':<12}"
        f"{'BM25':>12}"
        f"{'Semantic':>12}"
        f"{'Hybrid':>12}"
    )

    for metric in [
        "Recall@1",
        "Recall@3",
        "Recall@5",
        "Recall@10",
        "MRR",
    ]:
        print(
            f"{metric:<12}"
            f"{bm25_metrics[metric]:>12.3f}"
            f"{semantic_metrics[metric]:>12.3f}"
            f"{hybrid_metrics[metric]:>12.3f}"
        )


if __name__ == "__main__":
    main()
