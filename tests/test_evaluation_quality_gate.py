"""A quality threshold must gate complete results, not partial evaluations."""
import contextlib
import io
import unittest
from unittest.mock import patch
from scripts import evaluate_multirepo as command

CASE = {"repository": "fixture", "query": "example", "expected": "target"}


class EvaluationQualityGateTests(unittest.TestCase):
    def test_threshold_validation_precedes_database_access(self):
        for value in ("-0.1", "1.1", "nan", "inf", "-inf", "private-invalid"):
            with self.subTest(value=value), patch.object(command, "validate_test_cases") as validate, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    command.cli([f"--minimum-hybrid-recall-at-10={value}"])
                self.assertEqual(result.exception.code, 2)
                validate.assert_not_called()

    def test_gate_uses_hybrid_not_other_methods_and_accepts_equality(self):
        for recall, expected in ((.879, 1), (.880, 0), (.920, 0)):
            metrics = {key: 0 for key in ("Recall@1", "Recall@3", "Recall@5", "Recall@10", "MRR")}
            hybrid = {**metrics, "Recall@10": recall}
            output = io.StringIO()
            with self.subTest(recall=recall), \
                 patch.object(command, "validate_test_cases", return_value=([CASE], [])), \
                 patch.object(command, "evaluate", side_effect=[metrics, metrics, hybrid, metrics]) as evaluate, \
                 contextlib.redirect_stdout(output):
                self.assertEqual(command.cli(["--minimum-hybrid-recall-at-10", ".880"]), expected)
                self.assertEqual(evaluate.call_count, 4)
            self.assertIn("Quality gate failed" if expected else "Quality gate passed", output.getvalue())

    def test_incomplete_ground_truth_cannot_pass_gate(self):
        metrics = {key: 1 for key in ("Recall@1", "Recall@3", "Recall@5", "Recall@10", "MRR")}
        output = io.StringIO()
        with patch.object(command, "validate_test_cases", return_value=([CASE], [{**CASE, "reason": "missing"}])), \
             patch.object(command, "evaluate", return_value=metrics), contextlib.redirect_stdout(output):
            self.assertEqual(command.cli(["--minimum-hybrid-recall-at-10", "0"]), 1)
        self.assertNotIn("Quality gate passed", output.getvalue())

    def test_zero_and_one_are_valid_thresholds(self):
        self.assertEqual(command.recall_threshold("0"), 0)
        self.assertEqual(command.recall_threshold("1"), 1)
