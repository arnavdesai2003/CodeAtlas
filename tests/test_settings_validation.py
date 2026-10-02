"""Isolated startup settings validation; no .env or service reads."""
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from app.core.config import Settings


class SettingsValidationTests(unittest.TestCase):
    def create(self, **values):
        return Settings(_env_file=None, database_url="postgresql://test.invalid/db",
                        elasticsearch_url="http://test.invalid:9200",
                        redis_url="redis://test.invalid:6379", **values)

    def test_defaults_unchanged(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = self.create()
        self.assertEqual(settings.hybrid_semantic_weight, .60)
        self.assertEqual(settings.search_cache_ttl, 300)
        self.assertEqual(settings.request_body_timeout_seconds, 30)

    def test_weight_boundaries_and_positive_ttl_accepted(self):
        with patch.dict(os.environ, {}, clear=True):
            for weight in (0, .60, 1):
                settings = self.create(hybrid_semantic_weight=weight, search_cache_ttl=1)
                self.assertEqual(settings.hybrid_semantic_weight, weight)

    def test_invalid_numeric_values_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            for field, values in (("hybrid_semantic_weight", (-.01, 1.01, float("nan"), float("inf"))),
                                  ("search_cache_ttl", (0, -1)),
                                  ("request_body_timeout_seconds", (0, float("nan"), float("inf")))):
                for value in values:
                    with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                        self.create(**{field: value})

    def test_environment_values_are_validated_at_startup(self):
        for field, value in (("HYBRID_SEMANTIC_WEIGHT", "NaN"), ("HYBRID_SEMANTIC_WEIGHT", "1.5"),
                             ("SEARCH_CACHE_TTL", "0"), ("SEARCH_CACHE_TTL", "-1")):
            with patch.dict(os.environ, {field: value}, clear=True), self.assertRaises(ValidationError):
                self.create()

    def test_formatted_validation_errors_do_not_echo_inputs(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValidationError) as caught:
                self.create(search_cache_ttl="private-configuration-text")
        self.assertNotIn("private-configuration-text", str(caught.exception))
        self.assertIn("search_cache_ttl", str(caught.exception))
