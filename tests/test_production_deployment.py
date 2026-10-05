"""Production packaging must isolate stores and preserve existing credentials."""
import argparse
import stat
import tempfile
import unittest
from pathlib import Path
import yaml
from scripts.init_production_env import create_environment, hostname

ROOT = Path(__file__).resolve().parents[1]


class ProductionDeploymentTests(unittest.TestCase):
    def test_only_https_proxy_has_public_ports(self):
        config = yaml.safe_load((ROOT / "compose.production.yaml").read_text())
        self.assertEqual([name for name, service in config["services"].items() if service.get("ports")], ["caddy"])
        self.assertTrue(config["networks"]["backend"]["internal"])
        for name in ("postgres", "elasticsearch", "redis"):
            self.assertEqual(config["services"][name]["networks"], ["backend"])
        environment = config["services"]["api"]["environment"]
        self.assertEqual(environment["APP_ENV"], "production")
        self.assertIn(":?", environment["API_KEY"])
        self.assertEqual(environment["BENCHMARK_CACHE_BYPASS_ENABLED"], "false")

    def test_new_credentials_are_private_independent_and_url_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "environment"
            create_environment("api.codeatlas.org", path)
            values = dict(line.split("=", 1) for line in path.read_text().splitlines())
            keys = [values[key] for key in ("POSTGRES_PASSWORD", "API_KEY", "GITHUB_WEBHOOK_SECRET")]
            self.assertEqual(len(set(keys)), 3)
            self.assertTrue(all(len(key) == 64 and all(char in "0123456789abcdef" for char in key) for key in keys))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                create_environment("api.codeatlas.org", path)
            self.assertEqual(dict(line.split("=", 1) for line in path.read_text().splitlines()), values)

    def test_existing_symlink_is_not_followed_or_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing"
            target.write_text("preserve")
            link = Path(directory) / "link"
            link.symlink_to(target)
            with self.assertRaises(FileExistsError):
                create_environment("api.codeatlas.org", link)
            self.assertEqual(target.read_text(), "preserve")
            self.assertTrue(link.is_symlink())

    def test_invalid_hostnames_fail_before_file_creation(self):
        for value in ("https://api.example.com", "api.example.com/path", "localhost", "127.0.0.1",
                      "api.example.com:443", "*.example.com", "bad..org", "api.invalid", "api.example.com"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                hostname(value)

    def test_build_context_is_whitelisted_and_api_runs_nonroot(self):
        ignore = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertEqual(ignore[0], "*")
        self.assertFalse(any(line.startswith("!") and (".env" in line or "data" in line or ".venv" in line) for line in ignore))
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("USER codeatlas", dockerfile)
        self.assertNotIn("COPY . ", dockerfile)
