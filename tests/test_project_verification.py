"""Release verification must fail closed and keep live work opt-in."""
import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest.mock import Mock, patch
from scripts import verify_project as command
from scripts import verify_api_smoke as smoke

JOURNALS = {"status": "observed", "read_only": True, **{field: [] for field in (
    "sync_jobs", "full_index_jobs", "publication_jobs",
    "conflicting_sync_and_full_repository_ids", "conflicting_publication_and_sync_repository_ids")}}
INVENTORY = {"cluster_uuid": "cluster", "active_index": "active", "issues": [],
             "indices": [{"index_name": "active", "index_uuid": "uuid", "exists": True, "documents": 1}]}


class ProjectVerificationTests(unittest.TestCase):
    def test_production_environment_is_isolated_only_for_offline_child(self):
        deployed = {"APP_ENV": "production", "API_KEY": "private-test-api",
                    "GITHUB_WEBHOOK_SECRET": "private-test-webhook",
                    "ELASTICSEARCH_API_KEY": "private-test-search",
                    "DATABASE_URL": "postgresql://private-test-db@deployed.invalid/db",
                    "REDIS_URL": "rediss://deployed.invalid:6379",
                    "ELASTICSEARCH_URL": "https://deployed.invalid:9243"}
        def run(args, env, timeout):
            if "unittest" in args:
                self.assertEqual(env["APP_ENV"], "test")
                for key in ("API_KEY", "GITHUB_WEBHOOK_SECRET", "ELASTICSEARCH_API_KEY"):
                    self.assertEqual(env[key], "")
                for key in ("DATABASE_URL", "REDIS_URL", "ELASTICSEARCH_URL"):
                    self.assertIn("offline.invalid", env[key])
                self.assertEqual(env["HF_HUB_OFFLINE"], "1")
            else:
                for key, value in deployed.items():
                    self.assertEqual(env[key], value)
            if "scripts.inspect_recovery_jobs" in args:
                return 0, json.dumps(JOURNALS)
            if "scripts.manage_index_generations" in args:
                return 0, json.dumps(INVENTORY)
            return 0, ""
        with patch.dict(os.environ, deployed), patch.object(command, "run_check", side_effect=run), \
             contextlib.redirect_stdout(io.StringIO()):
            command.verify(live=True, timeout=300)

    def test_help_and_invalid_timeout_do_not_run_checks(self):
        with patch.object(command, "verify") as verify:
            for args, status in ((["--help"], 0), (["--timeout", "59"], 2)):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        command.main(args)
                    self.assertEqual(result.exception.code, status)
            verify.assert_not_called()

    def test_default_runs_offline_only(self):
        with patch.object(command, "run_check", return_value=(0, "")) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            report = command.verify(live=False, timeout=300)
        self.assertEqual(report["checks"], ["offline_tests"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(report["scope"], "offline_only")

    def test_live_checks_run_in_order_with_offline_models(self):
        def run(args, env, timeout):
            if "scripts.inspect_recovery_jobs" in args:
                return 0, json.dumps(JOURNALS)
            if "scripts.manage_index_generations" in args:
                return 0, json.dumps(INVENTORY)
            if "unittest" not in args:
                self.assertEqual(env["HF_HUB_OFFLINE"], "1")
                self.assertEqual(env["TORCH_NUM_THREADS"], "1")
            return 0, ""
        with patch.object(command, "run_check", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            report = command.verify(live=True, timeout=300)
        self.assertEqual(report["checks"], [name for name, _ in command.OFFLINE + command.LIVE])
        self.assertFalse(report["research_completed"])

    def test_changed_active_identity_fails_final_verification(self):
        inventories = iter([INVENTORY, {**INVENTORY, "cluster_uuid": "replaced"}])
        def run(args, env, timeout):
            if "scripts.inspect_recovery_jobs" in args:
                return 0, json.dumps(JOURNALS)
            if "scripts.manage_index_generations" in args:
                return 0, json.dumps(next(inventories))
            return 0, ""
        with patch.object(command, "run_check", side_effect=run), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "corpus changed"):
            command.verify(live=True, timeout=300)

    def test_pending_or_malformed_journals_stop_later_checks(self):
        for report in ({}, {**JOURNALS, "sync_jobs": [{"repository_id": 1}]}):
            with patch.object(command, "run_check", side_effect=[(0, ""), (0, json.dumps(report))]) as run, \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                command.verify(live=True, timeout=300)
            self.assertEqual(run.call_count, 2)

    def test_failures_stop_and_only_report_known_scratch_fields(self):
        output = io.StringIO()
        payload = json.dumps({"status": "starting", "private_namespace": "scratch:", "private_detail": "secret"})
        with patch.object(command, "run_check", return_value=(1, payload)) as run, \
             contextlib.redirect_stdout(output), self.assertRaises(RuntimeError):
            command.verify(live=True, timeout=300)
        self.assertEqual(run.call_count, 1)
        self.assertIn("scratch:", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_timeout_terminates_then_kills_only_owned_process_group(self):
        process = Mock(pid=12345)
        process.communicate.side_effect = [subprocess.TimeoutExpired("probe", 60),
                                          subprocess.TimeoutExpired("probe", 5), ("scratch output", "")]
        with patch.object(command.subprocess, "Popen", return_value=process) as start, \
             patch.object(command.os, "killpg") as stop:
            status, output = command.run_check(["-m", "fixture"], {}, 60)
        self.assertNotEqual(status, 0)
        self.assertEqual(output, "scratch output")
        self.assertEqual([call.args[0] for call in stop.call_args_list], [12345, 12345])
        self.assertTrue(start.call_args.kwargs["start_new_session"])


class ApiSmokeCommandTests(unittest.TestCase):
    def test_help_precedes_store_access(self):
        with patch.object(smoke, "verify_api_smoke") as verify, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                smoke.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        verify.assert_not_called()

    def test_failure_is_sanitized_and_success_is_preserved(self):
        output = io.StringIO()
        with patch.object(smoke, "verify_api_smoke", side_effect=RuntimeError("secret")), \
             contextlib.redirect_stdout(output):
            self.assertEqual(smoke.main([]), 1)
        self.assertNotIn("secret", output.getvalue())
        with patch.object(smoke, "verify_api_smoke", return_value={"status": "passed"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main([]), 0)

    def test_api_failure_still_closes_client_and_removes_private_keys(self):
        from app.search import cache
        client = Mock()
        client.get.return_value = "unchanged"
        http = Mock()
        http.get.return_value.status_code = 503
        with patch.object(cache, "redis_client", client), \
             patch("fastapi.testclient.TestClient", return_value=http), \
             patch("scripts.verify_cache_protocol._clear_namespace") as clear, \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            smoke.verify_api_smoke()
        http.close.assert_called_once()
        clear.assert_called_once()
        self.assertTrue(clear.call_args.args[1].startswith("codeatlas:verification:api:"))
