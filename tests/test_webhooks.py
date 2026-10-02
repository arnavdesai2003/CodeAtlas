"""Offline webhook authentication and scheduling boundaries."""

import hashlib
import hmac
import json
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import webhooks
from app.api.body_limit import RequestBodyLimit


class WebhookTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(webhooks.router)
        app.add_middleware(RequestBodyLimit, max_bytes=1024)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        secret = patch.object(webhooks.settings, "github_webhook_secret", "test-secret")
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
        with patch.object(webhooks.settings, "github_webhook_secret", ""):
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
