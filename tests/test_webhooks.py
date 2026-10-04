"""Offline webhook authentication and scheduling boundaries."""

import hashlib
import hmac
import json
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api import webhooks
from app.api.body_limit import RequestBodyLimit


class WebhookTests(unittest.TestCase):
    def test_signed_nonstandard_json_constants_reject_before_work(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            for body in (constant, b'{"nested":[{"value":' + constant + b'}]}',
                         b'{"repository":{"clone_url":"https://github.com/o/r"},"value":' + constant + b'}'):
                with self.subTest(body=body):
                    response = self.send(body)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json(), {"detail": "Invalid webhook JSON."})
                    self.assert_no_work()

    def test_signature_check_precedes_nonstandard_constant_parsing(self):
        with patch.object(webhooks.json, "loads") as loads:
            response = self.send(b'{"value":NaN}', signature="sha256=bad")
        self.assertEqual(response.status_code, 401)
        loads.assert_not_called()
        self.assert_no_work()

    def test_constant_names_in_strings_and_keys_remain_valid_json(self):
        body = b'{"NaN":"Infinity","nested":["-Infinity",null,true,1.5]}'
        response = self.send(body, event="ping")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "ok", "event": "ping"})
        self.assert_no_work()

    def test_signed_decoder_recursion_is_bad_request_before_database(self):
        depth = 10000
        body = b'{"nested":' + b'[' * depth + b'0' + b']' * depth + b'}'
        signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        app = FastAPI()
        app.include_router(webhooks.router)
        app.add_middleware(RequestBodyLimit, max_bytes=len(body))
        with TestClient(app) as client:
            response = client.post("/webhooks/github", content=body, headers={
                "X-Hub-Signature-256": signature, "X-GitHub-Event": "push",
            })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Invalid webhook JSON."})
        self.assert_no_work()

    def test_signed_duplicate_fields_reject_before_lookup_and_schedule(self):
        for body in (b'{"repository":{},"repository":{"clone_url":"https://github.com/o/r"}}',
                     b'{"repository":{"clone_url":"one","clone_url":"two"}}',
                     b'{"nested":{"field":1,"field":2}}'):
            with self.subTest(body=body):
                response = self.send(body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"detail": "Invalid webhook JSON."})
                self.assert_no_work()

    def test_invalid_signature_precedes_duplicate_field_parsing(self):
        with patch.object(webhooks.json, "loads") as loads:
            response = self.send(b'{"repository":{},"repository":{}}', signature="sha256=bad")
        self.assertEqual(response.status_code, 401)
        loads.assert_not_called()
        self.assert_no_work()

    def setUp(self):
        app = FastAPI()
        app.include_router(webhooks.router)
        app.add_middleware(RequestBodyLimit, max_bytes=1024)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        secret = patch.object(webhooks.settings, "github_webhook_secret", SecretStr("test-secret"))
        secret.start()
        self.addCleanup(secret.stop)
        session = patch.object(webhooks, "SessionLocal")
        self.session = session.start()
        self.addCleanup(session.stop)
        sync = patch.object(webhooks, "sync_repository_background")
        self.sync = sync.start()
        self.addCleanup(sync.stop)

    def send(self, body=b'{}', event="push", secret="test-secret", signature=None):
        signature = signature or "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post("/webhooks/github", content=body, headers={
            "X-Hub-Signature-256": signature, "X-GitHub-Event": event})

    def assert_no_work(self):
        self.session.assert_not_called()
        self.sync.assert_not_called()

    def test_oversized_body_rejected_before_signature_and_database(self):
        with patch.object(webhooks, "verify_github_signature") as verify:
            response = self.send(b"x" * 1025)
        self.assertEqual(response.status_code, 413)
        verify.assert_not_called()
        self.assert_no_work()

    def test_exact_body_limit_retains_signed_whitespace(self):
        response = self.send(b"{}" + b" " * 1022, event="ping")
        self.assertEqual(response.status_code, 202)
        self.assert_no_work()

    def test_unconfigured_rejects_empty_secret_forgery(self):
        with patch.object(webhooks.settings, "github_webhook_secret", SecretStr("")):
            response = self.send(secret="")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "GitHub webhook is not configured."})
        self.assert_no_work()

    def test_missing_and_tampered_signatures_reject_before_json(self):
        response = self.client.post("/webhooks/github", content=b"not JSON")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.send(b"not JSON", signature="sha256=bad").status_code, 401)
        self.assert_no_work()

    def test_non_ascii_signature_rejects_without_compare_error(self):
        with self.assertRaises(webhooks.HTTPException) as caught:
            webhooks.verify_github_signature(b"{}", "sha256=é")
        self.assertEqual(caught.exception.status_code, 401)

    def test_signed_bad_json_and_nonobject_payloads_are_400(self):
        for body in (b"not JSON", b"\xff", b"[]", b"null", b'"string"'):
            with self.subTest(body=body):
                self.assertEqual(self.send(body).status_code, 400)
        self.assert_no_work()

    def test_push_repository_shape_is_validated(self):
        for repository in (None, [], "string", {}, {"clone_url": 123}, {"clone_url": "  "}):
            with self.subTest(repository=repository):
                self.assertEqual(self.send(json.dumps({"repository": repository}).encode()).status_code, 400)
        self.assert_no_work()

    def test_ping_and_ignored_events_do_not_schedule_work(self):
        self.assertEqual(self.send(event="ping").json(), {"status": "ok", "event": "ping"})
        self.assertEqual(self.send(event="issues").json(), {"status": "ignored", "event": "issues"})
        self.assert_no_work()

    def test_valid_push_schedules_only_registered_repository(self):
        db = self.session.return_value
        db.query.return_value.filter.return_value.first.return_value = Mock(id=7)
        response = self.send(b'{"repository":{"clone_url":"https://github.com/owner/repo.git"}}')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["repository_id"], 7)
        self.sync.assert_called_once_with(7)
        db.close.assert_called_once()


    def test_unknown_repository_does_not_schedule_work(self):
        db = self.session.return_value
        db.query.return_value.filter.return_value.first.return_value = None
        response = self.send(b'{"repository":{"clone_url":"https://github.com/owner/repo.git"}}')
        self.assertEqual(response.status_code, 404)
        self.sync.assert_not_called()
        db.close.assert_called_once()


class WebhookBackgroundTests(unittest.TestCase):
    def test_session_creation_failure_is_recorded_without_raw_details(self):
        with patch.object(webhooks, "SessionLocal", side_effect=RuntimeError("private connection URL")), \
             patch.object(webhooks, "sync_repository") as sync, \
             self.assertLogs(webhooks.logger, level="ERROR") as logs:
            webhooks.sync_repository_background(7)
        sync.assert_not_called()
        self.assertIn("repository_id=7 error_type=RuntimeError", logs.output[0])
        self.assertNotIn("private connection URL", str(logs.output))

    def test_sync_failure_closes_session_and_does_not_log_exception_body(self):
        with patch.object(webhooks, "SessionLocal") as session, \
             patch.object(webhooks, "sync_repository", side_effect=ValueError("private token")), \
             self.assertLogs(webhooks.logger, level="ERROR") as logs:
            webhooks.sync_repository_background(8)
        session.return_value.close.assert_called_once()
        self.assertIn("error_type=ValueError", logs.output[0])
        self.assertNotIn("private token", str(logs.output))

    def test_session_close_failure_is_recorded_without_escaping(self):
        with patch.object(webhooks, "SessionLocal") as session, \
             patch.object(webhooks, "sync_repository", return_value={"private": "details"}), \
             self.assertLogs(webhooks.logger, level="INFO") as logs:
            session.return_value.close.side_effect = RuntimeError("private close URL")
            webhooks.sync_repository_background(9)
        self.assertIn("sync completed repository_id=9", logs.output[0])
        self.assertIn("session close failed repository_id=9 error_type=RuntimeError", logs.output[1])
        self.assertNotIn("private", str(logs.output))
