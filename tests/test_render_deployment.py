"""Render connection formats and startup behavior, without network or models."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from app.core.config import Settings
from scripts.start_api import server_arguments, main


class RenderDeploymentTests(unittest.TestCase):
    def settings(self, database, search="search-internal:9200", **extra):
        with patch.dict(os.environ, {}, clear=True):
            return Settings(_env_file=None, database_url=database,
                            elasticsearch_url=search, redis_url="rediss://cache.invalid:6379/0", **extra)

    def test_render_postgres_urls_use_installed_driver_without_corrupting_credentials(self):
        suffix = "user:p%40ss%2Fword@database.internal:5432/codeatlas?sslmode=require"
        for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://"):
            self.assertEqual(self.settings(prefix + suffix).database_url, "postgresql+psycopg://" + suffix)
        self.assertEqual(self.settings("sqlite:///:memory:").database_url, "sqlite:///:memory:")

    def test_private_search_reference_and_external_tls_are_supported(self):
        self.assertEqual(self.settings("sqlite://").elasticsearch_url, "http://search-internal:9200")
        settings = self.settings("sqlite://", "https://search.invalid:9243", elasticsearch_api_key="secret-for-test")
        self.assertEqual(settings.elasticsearch_url, "https://search.invalid:9243")
        self.assertNotIn("secret-for-test", repr(settings))
        self.assertEqual(settings.redis_url, "rediss://cache.invalid:6379/0")

    def test_environment_overrides_local_dotenv_and_connections_are_required(self):
        environment = {"DATABASE_URL": "postgresql://user:fake@database.internal/db",
                       "ELASTICSEARCH_URL": "search.internal:9200", "REDIS_URL": "redis://cache.internal:6379"}
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.database_url, "postgresql+psycopg://user:fake@database.internal/db")
        self.assertEqual(settings.elasticsearch_url, "http://search.internal:9200")
        self.assertEqual(settings.redis_url, environment["REDIS_URL"])

    def test_launcher_binds_all_interfaces_on_assigned_port_and_execs(self):
        arguments = server_arguments({"PORT": "10000"})
        self.assertEqual(arguments[arguments.index("--host") + 1], "0.0.0.0")
        self.assertEqual(arguments[arguments.index("--port") + 1], "10000")
        self.assertIn("--no-proxy-headers", arguments)
        with patch.dict(os.environ, {"PORT": "10000"}, clear=True), patch("scripts.start_api.os.execv") as execute:
            main()
        execute.assert_called_once_with(sys.executable, arguments)
        self.assertEqual(server_arguments({})[server_arguments({}).index("--port") + 1], "8000")

    def test_bad_ports_fail_before_startup_without_echoing_input(self):
        for value in ("", "0", "65536", "-1", "1;private", " 10000", "１", "1.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server_arguments({"PORT": value})

    def test_blueprint_keeps_search_persistence_private_stores_and_manual_deploys(self):
        root = Path(__file__).resolve().parents[1]
        blueprint = yaml.safe_load((root / "render.yaml").read_text())
        services = {row["name"]: row for row in blueprint["services"]}
        api = services["codeatlas-api"]
        search = services["codeatlas-search"]
        cache = services["codeatlas-cache"]
        database = blueprint["databases"][0]
        self.assertEqual(set(services), {"codeatlas-api", "codeatlas-search", "codeatlas-cache"})
        self.assertEqual(api["autoDeployTrigger"], "off")
        self.assertEqual(api["healthCheckPath"], "/health")
        self.assertEqual(api["disk"]["mountPath"], "/app/data")
        self.assertEqual(search["type"], "pserv")
        self.assertEqual(search["image"]["url"], "docker.elastic.co/elasticsearch/elasticsearch:9.4.3")
        self.assertEqual(search["disk"]["mountPath"], "/usr/share/elasticsearch/data")
        self.assertEqual(cache["type"], "keyvalue")
        self.assertEqual(cache["ipAllowList"], [])
        self.assertEqual(database["ipAllowList"], [])
        self.assertEqual(database["postgresMajorVersion"], "17")
        variables = {row["key"]: row for row in api["envVars"]}
        for name in ("API_KEY", "GITHUB_WEBHOOK_SECRET"):
            self.assertEqual(variables[name], {"key": name, "sync": False})
        for name in ("DATABASE_URL", "REDIS_URL", "ELASTICSEARCH_URL"):
            self.assertNotIn("value", variables[name])
        self.assertEqual(variables["HF_HOME"]["value"], "/app/data/model-cache")
        self.assertEqual(variables["BENCHMARK_CACHE_BYPASS_ENABLED"]["value"], "false")
