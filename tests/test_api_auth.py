"""Offline API access checks; no live stores or secrets."""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api import routes


class ApiAuthTests(unittest.TestCase):
    def test_nonboolean_elasticsearch_ack_cannot_report_healthy(self):
        with patch.object(routes, "engine"), \
             patch.object(routes, "elasticsearch_client") as es, \
             patch.object(routes, "redis_client") as redis:
            redis.ping.return_value = True
            for value in (False, None, 0, 1, "private-ping-payload", {"acknowledged": True}):
                with self.subTest(value=value):
                    es.ping.return_value = value
                    response = self.client.get("/health")
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["dependencies"]["elasticsearch"], {"status": "unhealthy"})
                    self.assertNotIn("private-ping-payload", response.text)

    def test_nonpositive_redis_ack_reports_unhealthy_without_raw_payload(self):
        with patch.object(routes, "engine"), \
             patch.object(routes, "elasticsearch_client") as es, \
             patch.object(routes, "redis_client") as redis:
            es.ping.return_value = True
            for value in (False, None, 1, "private-ping-payload"):
                with self.subTest(value=value):
                    redis.ping.return_value = value
                    response = self.client.get("/health")
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["dependencies"]["redis"], {"status": "unhealthy"})
                    self.assertNotIn("private-ping-payload", response.text)

    def test_openapi_describes_api_key_only_for_protected_routes(self):
        with patch.object(routes.settings, "api_key", SecretStr("private-schema-secret")):
            schema = self.client.get("/openapi.json").json()
        scheme = schema["components"]["securitySchemes"]["CodeAtlasAPIKey"]
        self.assertEqual((scheme["type"], scheme["in"], scheme["name"]),
                         ("apiKey", "header", "X-CodeAtlas-API-Key"))
        for method, path, _ in self.requests:
            operation = schema["paths"][path.replace("/1/", "/{repository_id}/")][method.lower()]
            self.assertEqual(operation["security"], [{"CodeAtlasAPIKey": []}])
        self.assertNotIn("security", schema["paths"]["/health"]["get"])
        self.assertNotIn("private-schema-secret", str(schema))

    def test_application_schema_keeps_health_root_and_webhook_outside_api_key_scheme(self):
        from app.main import app
        schema = app.openapi()
        for path, method in (("/", "get"), ("/health", "get"), ("/webhooks/github", "post")):
            self.assertNotIn("security", schema["paths"][path][method])
        self.assertEqual(schema["paths"]["/search"]["post"]["security"], [{"CodeAtlasAPIKey": []}])

    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.requests = (("POST", "/search", {"query": "q"}),
                         ("POST", "/repositories", {"clone_url": "https://github.com/o/r"}),
                         ("GET", "/repositories", None),
                         ("POST", "/repositories/1/sync", None))

    def test_nonlocal_modes_fail_closed_without_key_before_work(self):
        def forbidden_db():
            raise AssertionError("Database dependency ran before authentication.")
        self.client.app.dependency_overrides[routes.get_db] = forbidden_db
        for environment in ("production", "staging", "prod"):
            with patch.object(routes.settings, "app_env", environment), \
                 patch.object(routes.settings, "api_key", SecretStr("")), \
                 patch.object(routes, "search_with_cache") as search:
                for method, path, payload in self.requests:
                    response = self.client.request(method, path, json=payload)
                    self.assertEqual(response.status_code, 503)
                search.assert_not_called()

    def test_configured_key_rejects_missing_and_wrong_before_work(self):
        with patch.object(routes.settings, "api_key", SecretStr("test-private-key")), \
             patch.object(routes, "search_with_cache") as search:
            for method, path, payload in self.requests:
                for headers in ({}, {"X-CodeAtlas-API-Key": "wrong"}):
                    response = self.client.request(method, path, json=payload, headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertNotIn("test-private-key", response.text)
            search.assert_not_called()

    def test_valid_key_allows_search_without_changing_inputs(self):
        with patch.object(routes.settings, "app_env", "production"), \
             patch.object(routes.settings, "api_key", SecretStr("test-private-key")), \
             patch.object(routes, "search_with_cache", return_value={"results": [],
                 "cache_hit": False, "search_latency_ms": 1}) as search:
            response = self.client.post("/search", json={"query": " q "},
                headers={"X-CodeAtlas-API-Key": "test-private-key"})
        self.assertEqual(response.status_code, 200)
        search.assert_called_once_with(query=" q ", limit=10, bypass_cache=False)

    def test_health_remains_accessible_with_configured_key(self):
        with patch.object(routes.settings, "api_key", SecretStr("test-private-key")), \
             patch.object(routes, "engine"), patch.object(routes, "elasticsearch_client") as es, \
             patch.object(routes, "redis_client") as redis:
            es.ping.return_value = True
            redis.ping.return_value = True
            self.assertEqual(self.client.get("/health").status_code, 200)

    def test_key_is_redacted_by_settings_type(self):
        self.assertNotIn("test-private-key", repr(SecretStr("test-private-key")))
