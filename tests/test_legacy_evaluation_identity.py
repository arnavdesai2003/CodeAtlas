"""Legacy micrograd scores must not credit symbols from another repository."""

import contextlib
import io
import unittest
from unittest.mock import patch

from scripts import evaluate_search, tune_hybrid


class LegacyEvaluationIdentityTests(unittest.TestCase):
    def test_all_legacy_cases_are_explicitly_micrograd(self):
        self.assertEqual(len(evaluate_search.TEST_CASES), 8)
        self.assertTrue(all(case["repository"] == "micrograd"
                            for case in evaluate_search.TEST_CASES))
        self.assertIs(tune_hybrid.TEST_CASES, evaluate_search.TEST_CASES)

    def test_both_tools_skip_wrong_repository_and_keep_actual_rank(self):
        case = {"repository": "micrograd", "expected": "Value.backward", "query": "gradient"}
        wrong = {"repository": "other", "qualified_name": case["expected"]}
        correct = {"repository": "micrograd", "qualified_name": case["expected"]}
        for results, score in (([wrong], 0), ([wrong, correct], .5), ([correct], 1)):
            with self.subTest(results=results), \
                 patch.object(evaluate_search, "TEST_CASES", [case]), \
                 patch.object(tune_hybrid, "TEST_CASES", [case]), \
                 contextlib.redirect_stdout(io.StringIO()):
                calls = []

                def search(**kwargs):
                    calls.append(kwargs)
                    return results

                legacy = evaluate_search.evaluate(search, "fixture")
                with patch.object(tune_hybrid, "hybrid_search_weighted", search):
                    tuning = tune_hybrid.evaluate_weight(.60)
            self.assertEqual(legacy["MRR"], score)
            self.assertEqual(tuning["mrr"], score)
            self.assertEqual(legacy["Recall@1"], int(score == 1))
            self.assertEqual(tuning["recall_1"], int(score == 1))
            self.assertEqual(calls, [{"query": "gradient", "limit": 10},
                                     {"query": "gradient", "limit": 10, "semantic_weight": .60}])
