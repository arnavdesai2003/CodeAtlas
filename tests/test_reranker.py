"""Deterministic model-output validation without loading a cross-encoder."""
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.search import reranker
from app.search.errors import InvalidRerankerOutputError


def candidates():
    return [dict(repository="repo", path=f"{name}.py", qualified_name=name,
                 kind="function", code=f"def {name}(): pass") for name in ("first", "second", "third")]


class RerankerTests(unittest.TestCase):
    def test_concurrent_cold_requests_construct_one_shared_model(self):
        reranker._load_reranker.cache_clear()
        self.addCleanup(reranker._load_reranker.cache_clear)
        ready = Event()
        lock = Lock()
        attempts = 0
        model = Mock()

        def construct(*args):
            self.assertTrue(ready.wait(timeout=5))
            return model

        def request(_):
            nonlocal attempts
            with lock:
                attempts += 1
                if attempts == 6:
                    ready.set()
            return reranker.get_reranker()

        with patch.object(reranker, "CrossEncoder", side_effect=construct) as constructor, \
             ThreadPoolExecutor(max_workers=6) as executor:
            models = list(executor.map(request, range(6)))
        constructor.assert_called_once_with(reranker.RERANKER_MODEL)
        self.assertTrue(all(result is model for result in models))

    def test_failed_initialization_is_not_cached_or_left_locked(self):
        reranker._load_reranker.cache_clear()
        self.addCleanup(reranker._load_reranker.cache_clear)
        model = Mock()
        with patch.object(reranker, "CrossEncoder", side_effect=[RuntimeError("load failed"), model]) as constructor:
            with self.assertRaises(RuntimeError):
                reranker.get_reranker()
            self.assertIs(reranker.get_reranker(), model)
            self.assertIs(reranker.get_reranker(), model)
        self.assertEqual(constructor.call_count, 2)

    def test_prediction_remains_concurrent_after_initialization(self):
        reranker._load_reranker.cache_clear()
        self.addCleanup(reranker._load_reranker.cache_clear)
        gate = Barrier(2)
        model = Mock()

        def predict(pairs, **kwargs):
            gate.wait(timeout=5)
            return [0.5] * len(pairs)

        model.predict.side_effect = predict
        with patch.object(reranker, "CrossEncoder", return_value=model) as constructor, \
             ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: reranker.rerank_results("q", candidates(), 3), range(2)))
        constructor.assert_called_once()
        self.assertEqual([len(result) for result in outcomes], [3, 3])

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
