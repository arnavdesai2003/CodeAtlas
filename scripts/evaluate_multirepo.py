from __future__ import annotations
import argparse

from app.db.database import SessionLocal
from app.db.models import CodeSymbol, Repository
from app.search.engine import (
    bm25_search,
    semantic_search,
    hybrid_search,
)

from app.search.engine import (
    bm25_search,
    semantic_search,
    hybrid_search,
    reranked_hybrid_search,
)

# ---------------------------------------------------------------------
# Held-out evaluation queries
#
# These queries are intentionally phrased differently from symbol names.
# We also specify the repository to prevent an identically named symbol
# in another project from being treated as correct.
# ---------------------------------------------------------------------

TEST_CASES = [
    # ---------------------------------------------------------------
    # MICROGRAD
    # ---------------------------------------------------------------
    {
        "repository": "micrograd",
        "query": "compute gradients using reverse mode automatic differentiation",
        "expected": "Value.backward",
    },
    {
        "repository": "micrograd",
        "query": "reset every trainable parameter gradient to zero",
        "expected": "Module.zero_grad",
    },
    {
        "repository": "micrograd",
        "query": "apply rectified linear activation to a scalar value",
        "expected": "Value.relu",
    },
    {
        "repository": "micrograd",
        "query": "return all trainable parameters from the neural network module",
        "expected": "Module.parameters",
    },
    {
        "repository": "micrograd",
        "query": "multi layer perceptron neural network",
        "expected": "MLP",
    },

    # ---------------------------------------------------------------
    # HTTPX
    # ---------------------------------------------------------------
    {
        "repository": "httpx",
        "query": "generate authentication requests for HTTP digest authentication",
        "expected": "DigestAuth.auth_flow",
    },
    {
        "repository": "httpx",
        "query": "construct the digest authentication authorization header",
        "expected": "DigestAuth._build_auth_header",
    },
    {
        "repository": "httpx",
        "query": "parse a digest authentication challenge from the server",
        "expected": "DigestAuth._parse_challenge",
    },
    {
        "repository": "httpx",
        "query": "check whether a redirect remains on HTTPS",
        "expected": "_is_https_redirect",
    },
    {
        "repository": "httpx",
        "query": "determine whether two URLs belong to the same origin",
        "expected": "_same_origin",
    },

    # ---------------------------------------------------------------
    # REQUESTS
    # ---------------------------------------------------------------
    {
        "repository": "requests",
        "query": "send an HTTP request using a persistent session",
        "expected": "Session.request",
    },
    {
        "repository": "requests",
        "query": "perform an HTTP GET using an existing session",
        "expected": "Session.get",
    },
    {
        "repository": "requests",
        "query": "throw an exception when an HTTP response indicates failure",
        "expected": "Response.raise_for_status",
    },
    {
        "repository": "requests",
        "query": "decode a response body as JSON",
        "expected": "Response.json",
    },
    {
        "repository": "requests",
        "query": "prepare an HTTP request before sending it",
        "expected": "Session.prepare_request",
    },

    # ---------------------------------------------------------------
    # ITSDANGEROUS
    # ---------------------------------------------------------------
    {
        "repository": "itsdangerous",
        "query": "cryptographically sign a byte string",
        "expected": "Signer.sign",
    },
    {
        "repository": "itsdangerous",
        "query": "verify and remove a cryptographic signature",
        "expected": "Signer.unsign",
    },
    {
        "repository": "itsdangerous",
        "query": "serialize an object into signed data",
        "expected": "Serializer.dumps",
    },
    {
        "repository": "itsdangerous",
        "query": "deserialize and verify signed serialized data",
        "expected": "Serializer.loads",
    },

    # ---------------------------------------------------------------
    # MARKUPSAFE
    # ---------------------------------------------------------------
    {
        "repository": "markupsafe",
        "query": "escape unsafe HTML characters",
        "expected": "escape",
    },
    {
        "repository": "markupsafe",
        "query": "represent text that is already safe for HTML output",
        "expected": "Markup",
    },
    {
        "repository": "markupsafe",
        "query": "convert an arbitrary value into a string without escaping",
        "expected": "soft_str",
    },

    # ---------------------------------------------------------------
    # CLICK
    # ---------------------------------------------------------------
    {
        "repository": "click",
        "query": "execute a command line command as the main entry point",
        "expected": "Command.main",
    },
    {
        "repository": "click",
        "query": "parse command line arguments for a command",
        "expected": "Command.parse_args",
    },
    {
        "repository": "click",
        "query": "invoke a click command with its current context",
        "expected": "Command.invoke",
    },
]


# ---------------------------------------------------------------------
# Ground-truth validation
# ---------------------------------------------------------------------

def validate_test_cases() -> tuple[list[dict], list[dict]]:
    """
    Verify that every expected symbol really exists in the PostgreSQL
    corpus.

    Invalid cases are excluded instead of silently corrupting metrics.
    """

    db = SessionLocal()

    valid_cases = []
    invalid_cases = []

    try:
        for case in TEST_CASES:
            repositories = (
                db.query(Repository)
                .filter(
                    Repository.name
                    == case["repository"]
                )
                .all()
            )

            if not repositories:
                invalid_cases.append(
                    {
                        **case,
                        "reason": "repository not found",
                    }
                )
                continue

            if len(repositories) != 1:
                invalid_cases.append({**case, "reason": "repository name is ambiguous"})
                continue
            repository = repositories[0]

            symbol = (
                db.query(CodeSymbol)
                .filter(
                    CodeSymbol.repository_id
                    == repository.id,
                    CodeSymbol.qualified_name
                    == case["expected"],
                )
                .first()
            )

            if symbol is None:
                invalid_cases.append(
                    {
                        **case,
                        "reason": "symbol not found",
                    }
                )
                continue

            valid_cases.append(case)

    finally:
        db.close()

    return valid_cases, invalid_cases


# ---------------------------------------------------------------------
# Result matching
# ---------------------------------------------------------------------

def result_is_correct(
    result: dict,
    case: dict,
) -> bool:
    return (
        result["repository"]
        == case["repository"]
        and
        result["qualified_name"]
        == case["expected"]
    )


def find_rank(
    results: list[dict],
    case: dict,
) -> int | None:
    for rank, result in enumerate(
        results,
        start=1,
    ):
        if result_is_correct(
            result,
            case,
        ):
            return rank

    return None


# ---------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------

def evaluate(
    search_function,
    name: str,
    test_cases: list[dict],
) -> dict:
    ranks = []

    print()
    print(name)
    print("=" * 110)

    for index, case in enumerate(
        test_cases,
        start=1,
    ):
        results = search_function(
            query=case["query"],
            limit=10,
        )

        rank = find_rank(
            results,
            case,
        )

        ranks.append(rank)

        rank_display = (
            str(rank)
            if rank is not None
            else "MISS"
        )

        print(
            f"{index:>2}. "
            f"[{case['repository']:<12}] "
            f"{case['query']:<60} "
            f"target={case['expected']:<30} "
            f"rank={rank_display}"
        )

    total = len(test_cases)

    if total == 0:
        raise RuntimeError(
            "No valid evaluation cases exist."
        )

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
        1 / rank if rank is not None else 0
        for rank in ranks
    ) / total

    print()
    print(f"Queries   : {total}")
    print(f"Recall@1  : {recall_1:.3f}")
    print(f"Recall@3  : {recall_3:.3f}")
    print(f"Recall@5  : {recall_5:.3f}")
    print(f"Recall@10 : {recall_10:.3f}")
    print(f"MRR       : {mrr:.3f}")

    return {
        "queries": total,
        "Recall@1": recall_1,
        "Recall@3": recall_3,
        "Recall@5": recall_5,
        "Recall@10": recall_10,
        "MRR": mrr,
    }


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main(argv=None):
    argparse.ArgumentParser(description="Evaluate four retrieval methods against validated multi-repository cases.").parse_args(argv)
    valid_cases, invalid_cases = (
        validate_test_cases()
    )

    print()
    print("=" * 110)
    print("CODEATLAS MULTI-REPOSITORY EVALUATION")
    print("=" * 110)

    print(
        f"Defined cases : {len(TEST_CASES)}"
    )

    print(
        f"Valid cases   : {len(valid_cases)}"
    )

    print(
        f"Invalid cases : {len(invalid_cases)}"
    )

    if invalid_cases:
        print()
        print("INVALID TEST CASES")
        print("-" * 110)

        for case in invalid_cases:
            print(
                f"[{case['repository']}] "
                f"{case['expected']} "
                f"-> {case['reason']}"
            )

    print()
    print(
        "Only validated ground-truth cases "
        "will be included in the metrics."
    )
    if not valid_cases:
        print("No validated cases; retrieval evaluation skipped.")
        return 1

    bm25_metrics = evaluate(
        bm25_search,
        "BM25",
        valid_cases,
    )

    semantic_metrics = evaluate(
        semantic_search,
        "SEMANTIC",
        valid_cases,
    )

    hybrid_metrics = evaluate(
        hybrid_search,
        "HYBRID",
        valid_cases,
    )
    
    reranked_metrics = evaluate(
    reranked_hybrid_search,
    "HYBRID + RERANKER",
    valid_cases,
    )

    print()
    print("=" * 80)
    print("FINAL SUMMARY")
    print("=" * 80)

    print(
    f"{'Metric':<15}"
    f"{'BM25':>15}"
    f"{'Semantic':>15}"
    f"{'Hybrid':>15}"
    f"{'Reranked':>15}"
    )

    for metric in [
        "Recall@1",
        "Recall@3",
        "Recall@5",
        "Recall@10",
        "MRR",
    ]:
        print(
    f"{metric:<15}"
    f"{bm25_metrics[metric]:>15.3f}"
    f"{semantic_metrics[metric]:>15.3f}"
    f"{hybrid_metrics[metric]:>15.3f}"
    f"{reranked_metrics[metric]:>15.3f}"
    )
    if invalid_cases:
        print("Evaluation incomplete: invalid ground-truth cases were excluded; subset metrics are not a full verification.")
        return 1
    return 0


def cli(argv=None):
    try:
        return main(argv)
    except Exception as exc:
        print(f"{type(exc).__name__}: Evaluation failed; partial output is not a complete result.")
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
