"""Check evaluation scores against independently calculated ranked fixtures."""

import contextlib
import io
import unittest

from scripts.evaluate_multirepo import evaluate, find_rank


class EvaluationMetricsTests(unittest.TestCase):
    def test_rank_boundaries_and_misses_have_expected_scores(self):
        ranks = [1, 3, 5, 10, None]
        cases = [
            {"repository": "target", "expected": "symbol", "query": str(index)}
            for index in range(len(ranks))
        ]
        calls = []

        def search(*, query, limit):
            calls.append((query, limit))
            rank = ranks[int(query)]
            results = [{"repository": "other", "qualified_name": "symbol"}
                       for _ in range(10)]
            if rank is not None:
                results[rank - 1] = {"repository": "target", "qualified_name": "symbol"}
            return results

        with contextlib.redirect_stdout(io.StringIO()):
            metrics = evaluate(search, "fixture", cases)

        self.assertEqual(calls, [(str(index), 10) for index in range(5)])
        self.assertEqual(metrics["queries"], 5)
        for cutoff, expected in ((1, .2), (3, .4), (5, .6), (10, .8)):
            self.assertAlmostEqual(metrics[f"Recall@{cutoff}"], expected)
        self.assertAlmostEqual(metrics["MRR"], 49 / 150)

    def test_first_exact_hit_wins_without_collapsing_other_results(self):
        case = {"repository": "target", "expected": "symbol"}
        results = [
            {"repository": "other", "qualified_name": "symbol"},
            {"repository": "target", "qualified_name": "other"},
            {"repository": "target", "qualified_name": "symbol"},
            {"repository": "target", "qualified_name": "symbol"},
        ]
        self.assertEqual(find_rank(results, case), 3)
        self.assertIsNone(find_rank(results[:2], case))
        self.assertIsNone(find_rank([], case))

    def test_empty_case_set_cannot_report_metrics(self):
        def search(**kwargs):
            self.fail("empty evaluation must not retrieve")

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "No valid evaluation cases"):
                evaluate(search, "fixture", [])
