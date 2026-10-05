"""Legacy command help, failures and completeness must be trustworthy."""
import contextlib
import io
import unittest
from unittest.mock import patch
from scripts import evaluate_search, tune_hybrid


class LegacyEvaluationCommandTests(unittest.TestCase):
    def test_argument_handling_precedes_database_and_search(self):
        for module in (evaluate_search, tune_hybrid):
            with patch.object(module, "validate_test_cases") as validate:
                for argv, status in ((["--help"], 0), (["--unknown"], 2)):
                    with self.subTest(module=module.__name__, argv=argv), \
                         contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as outcome:
                            module.cli(argv)
                        self.assertEqual(outcome.exception.code, status)
                validate.assert_not_called()

    def test_invalid_or_empty_ground_truth_skips_all_retrieval(self):
        for module, function in ((evaluate_search, "evaluate"), (tune_hybrid, "evaluate_weight")):
            for ground_truth in (([], []), (module.TEST_CASES, [{"reason": "ambiguous"}])):
                with self.subTest(module=module.__name__, ground_truth=ground_truth), \
                     patch.object(module, "validate_test_cases", return_value=ground_truth) as validate, \
                     patch.object(module, function) as retrieve, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.cli([]), 1)
                    validate.assert_called_once_with(module.TEST_CASES)
                    retrieve.assert_not_called()

    def test_database_and_retrieval_errors_are_sanitized(self):
        for module, function, summary in ((evaluate_search, "evaluate", "SUMMARY"),
                                          (tune_hybrid, "evaluate_weight", "BEST CONFIGURATION")):
            for failed in ("validate_test_cases", function):
                output = io.StringIO()
                with self.subTest(module=module.__name__, failed=failed), \
                     patch.object(module, "validate_test_cases", return_value=(module.TEST_CASES, [])), \
                     patch.object(module, failed, side_effect=RuntimeError("private detail")) as failure, \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(module.cli([]), 1)
                failure.assert_called_once()
                self.assertNotIn("private detail", output.getvalue())
                self.assertNotIn(summary, output.getvalue())
                self.assertIn("partial output is not a complete result", output.getvalue())

    def test_complete_commands_keep_methods_and_weight_sweep(self):
        metrics = {key: .5 for key in ("Recall@1", "Recall@3", "Recall@5", "Recall@10", "MRR")}
        with patch.object(evaluate_search, "validate_test_cases", return_value=(evaluate_search.TEST_CASES, [])), \
             patch.object(evaluate_search, "evaluate", return_value=metrics) as evaluate, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(evaluate_search.cli([]), 0)
        self.assertEqual([call.args[1] for call in evaluate.call_args_list], ["BM25", "SEMANTIC", "HYBRID"])
        def scored(weight):
            return {"weight": weight, **{key: .5 for key in ("recall_1", "recall_3", "recall_5", "recall_10", "mrr")}}
        with patch.object(tune_hybrid, "validate_test_cases", return_value=(tune_hybrid.TEST_CASES, [])), \
             patch.object(tune_hybrid, "evaluate_weight", side_effect=scored) as evaluate, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tune_hybrid.cli([]), 0)
        self.assertEqual([call.args[0] for call in evaluate.call_args_list], tune_hybrid.WEIGHTS)

    def test_scoring_rejects_empty_cases_and_malformed_trailing_hits(self):
        for cases, results in (([], []), (evaluate_search.TEST_CASES[:1],
                    [{"repository": "micrograd", "qualified_name": "Value.backward"}, {}])):
            with patch.object(evaluate_search, "TEST_CASES", cases), \
                 patch.object(tune_hybrid, "TEST_CASES", cases), \
                 patch.object(tune_hybrid, "hybrid_search_weighted", return_value=results), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    evaluate_search.evaluate(lambda **kwargs: results, "fixture")
                with self.assertRaises(ValueError):
                    tune_hybrid.evaluate_weight(.6)
