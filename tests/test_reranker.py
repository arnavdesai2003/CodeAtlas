"""Deterministic model-output validation without loading a cross-encoder."""
import copy
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.search import reranker
from app.search.errors import InvalidRerankerOutputError


def candidates():
    return [dict(repository="repo", path=f"{name}.py", qualified_name=name,
                 kind="function", code=f"def {name}(): pass") for name in ("first", "second", "third")]


class RerankerTests(unittest.TestCase):
    def test_valid_numpy_scores_preserve_tie_order_and_original_candidates(self):
        results = candidates()
        original = copy.deepcopy(results)
        model = Mock()
        model.predict.return_value = np.array([0.25, 0.75, 0.75], dtype=np.float32)
        with patch.object(reranker, "get_reranker", return_value=model):
            output = reranker.rerank_results("query", results, 2)
        self.assertEqual([row["qualified_name"] for row in output], ["second", "third"])
        self.assertEqual([row["reranker_score"] for row in output], [.75, .75])
        self.assertEqual(results, original)
        self.assertIsNot(output[0], results[1])
        self.assertEqual(len(model.predict.call_args.args[0]), 3)

    def test_wrong_score_count_and_shape_fail_without_mutating_candidates(self):
        results = candidates()
        original = copy.deepcopy(results)
        for scores in (None, 1.0, [], [1, 2], [1, 2, 3, 4], np.ones((3, 1))):
            with self.subTest(shape=type(scores).__name__), patch.object(reranker, "get_reranker") as model:
                model.return_value.predict.return_value = scores
                with self.assertRaises(InvalidRerankerOutputError):
                    reranker.rerank_results("q", results, 3)
                self.assertEqual(results, original)

    def test_invalid_scores_fail_and_healthy_retry_succeeds(self):
        model = Mock()
        with patch.object(reranker, "get_reranker", return_value=model):
            for score in (float("nan"), float("inf"), -float("inf"), True,
                          np.bool_(True), "1.0", 10 ** 1000):
                model.predict.return_value = [1, 2, score]
                with self.subTest(score_type=type(score).__name__), self.assertRaises(InvalidRerankerOutputError):
                    reranker.rerank_results("q", candidates(), 3)
            model.predict.return_value = [-1, 0, 1]
            self.assertEqual(len(reranker.rerank_results("q", candidates(), 3)), 3)

    def test_empty_candidates_skip_model_loading(self):
        with patch.object(reranker, "get_reranker") as model:
            self.assertEqual(reranker.rerank_results("q", [], 10), [])
        model.assert_not_called()
