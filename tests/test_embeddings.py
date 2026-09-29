"""Configuration tests use fake models and never download model weights."""

import unittest
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep
from unittest.mock import Mock, patch

os.environ.update(
    DATABASE_URL="postgresql+psycopg://test:test@127.0.0.1/test",
    ELASTICSEARCH_URL="http://127.0.0.1:9200",
    REDIS_URL="redis://127.0.0.1:6379/15",
    HF_HUB_OFFLINE="1",
)
from app.search import embeddings
from app.core.config import Settings
from pydantic import ValidationError


class EmbeddingConfigurationTests(unittest.TestCase):
    def setUp(self):
        embeddings._load_embedding_model.cache_clear()
        self.addCleanup(embeddings._load_embedding_model.cache_clear)

    def test_default_keeps_automatic_device_and_thread_settings(self):
        with patch.object(embeddings.settings, "embedding_device", None), \
             patch.object(embeddings.settings, "torch_num_threads", None), \
             patch.object(embeddings, "SentenceTransformer") as model, \
             patch.object(embeddings.torch, "set_num_threads") as threads:
            self.assertIs(embeddings.get_embedding_model(), model.return_value)
        model.assert_called_once_with(embeddings.MODEL_NAME)
        threads.assert_not_called()

    def test_explicit_configuration_applies_before_model_creation_once(self):
        events = []
        with patch.object(embeddings.settings, "embedding_device", "cpu"), \
             patch.object(embeddings.settings, "torch_num_threads", 1), \
             patch.object(embeddings.torch, "set_num_threads", side_effect=lambda n: events.append(("threads", n))), \
             patch.object(embeddings, "SentenceTransformer", side_effect=lambda *a, **kw: events.append(("model", kw))) as model:
            embeddings.get_embedding_model()
            embeddings.get_embedding_model()
        self.assertEqual(events, [("threads", 1), ("model", {"device": "cpu"})])
        model.assert_called_once()

    def test_query_and_batch_keep_normalized_embeddings(self):
        model = Mock()
        model.encode.return_value.tolist.return_value = [0.1, 0.2]
        with patch.object(embeddings, "get_embedding_model", return_value=model):
            self.assertEqual(embeddings.embed_text("query"), [.1, .2])
            model.encode.assert_called_with("query", normalize_embeddings=True, show_progress_bar=False)
            embeddings.embed_texts(["source"], batch_size=8)
            model.encode.assert_called_with(["source"], batch_size=8, normalize_embeddings=True, show_progress_bar=True)

    def test_invalid_thread_count_rejected(self):
        for count in [0, -1]:
            with self.assertRaises(ValidationError):
                Settings(_env_file=None, torch_num_threads=count)

    def test_concurrent_cold_requests_load_one_model(self):
        barrier = Barrier(5)
        model = object()

        def create(*args, **kwargs):
            sleep(.01)
            return model

        def get(_):
            barrier.wait(timeout=2)
            return embeddings.get_embedding_model()

        with patch.object(embeddings.settings, "torch_num_threads", None), \
             patch.object(embeddings, "SentenceTransformer", side_effect=create) as constructor, \
             ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(get, range(5)))
        constructor.assert_called_once()
        self.assertTrue(all(result is model for result in results))


if __name__ == "__main__":
    unittest.main()
