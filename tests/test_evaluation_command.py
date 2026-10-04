"""Ground-truth exclusions must not produce successful verification exits."""
import io
from contextlib import redirect_stdout, redirect_stderr
import unittest
from unittest.mock import patch
from scripts import evaluate_multirepo as command

METRICS = {key: .5 for key in ("Recall@1", "Recall@3", "Recall@5", "Recall@10", "MRR")}
CASE = {"repository": "fixture", "query": "example", "expected": "target"}


class EvaluationCommandTests(unittest.TestCase):
    def test_invalid_cases_keep_subset_metrics_but_fail_verification(self):
        invalid = {**CASE, "reason": "symbol not found"}
        output = io.StringIO()
        with patch.object(command, "validate_test_cases", return_value=([CASE], [invalid])), \
             patch.object(command, "evaluate", return_value=METRICS) as evaluate, redirect_stdout(output):
            self.assertEqual(command.main([]), 1)
        self.assertEqual(evaluate.call_count, 4)
        for call in evaluate.call_args_list:
            self.assertEqual(call.args[2], [CASE])
        self.assertIn("FINAL SUMMARY", output.getvalue())
        self.assertIn("Evaluation incomplete", output.getvalue())

    def test_no_valid_cases_skip_all_retrieval(self):
        with patch.object(command, "validate_test_cases", return_value=([], [{**CASE, "reason": "repository not found"}])), \
             patch.object(command, "evaluate") as evaluate, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(command.main([]), 1)
        evaluate.assert_not_called()
        self.assertIn("retrieval evaluation skipped", output.getvalue())
        self.assertNotIn("FINAL SUMMARY", output.getvalue())

    def test_complete_ground_truth_success_preserves_all_methods(self):
        with patch.object(command, "validate_test_cases", return_value=([CASE], [])), \
             patch.object(command, "evaluate", return_value=METRICS) as evaluate, redirect_stdout(io.StringIO()):
            self.assertEqual(command.main([]), 0)
        self.assertEqual([call.args[1] for call in evaluate.call_args_list],
                         ["BM25", "SEMANTIC", "HYBRID", "HYBRID + RERANKER"])

    def test_help_and_unknown_arguments_precede_ground_truth_access(self):
        with patch.object(command, "validate_test_cases") as validate:
            for arguments, expected in ((["--help"], 0), (["--unsupported"], 2)):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        command.main(arguments)
                self.assertEqual(result.exception.code, expected)
            validate.assert_not_called()
