"""ASGI body-limit boundaries, without servers or dependencies."""
import unittest
import asyncio
from unittest.mock import AsyncMock

from app.api.body_limit import RequestBodyLimit


class BodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_body_deadline_cancels_receive_without_dispatch(self):
        app, send = AsyncMock(), AsyncMock()
        calls = 0
        cancelled = asyncio.Event()

        async def receive():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"type": "http.request", "body": b"a", "more_body": True}
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        await RequestBodyLimit(app, 4, timeout_seconds=.01)({"type": "http"}, receive, send)
        app.assert_not_called()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(send.call_args_list[0].args[0]["status"], 408)
        self.assertIn((b"connection", b"close"), send.call_args_list[0].args[0]["headers"])

    async def test_route_execution_is_outside_body_deadline(self):
        async def app(scope, receive, send):
            await asyncio.sleep(.02)
            await send({"type": "http.response.start", "status": 200, "headers": []})
        send = AsyncMock()
        await RequestBodyLimit(app, 4, timeout_seconds=.01)({"type": "http"},
            AsyncMock(return_value={"type": "http.request", "body": b"ok"}), send)
        self.assertEqual(send.call_args.args[0]["status"], 200)

    async def test_external_cancellation_is_not_reported_as_timeout(self):
        app, send = AsyncMock(), AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await RequestBodyLimit(app, 4)({"type": "http"},
                AsyncMock(side_effect=asyncio.CancelledError), send)
        app.assert_not_called()
        send.assert_not_called()
    async def run_body(self, chunks, *, limit=4, headers=()):
        messages = [{"type": "http.request", "body": chunk,
                     "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)]
        receive = AsyncMock(side_effect=messages)
        send = AsyncMock()
        observed = []

        async def app(scope, receive, send):
            observed.append(await receive())

        await RequestBodyLimit(app, limit)({"type": "http", "headers": headers}, receive, send)
        return observed, send, receive

    async def test_exact_limit_preserves_raw_bytes_across_chunks(self):
        observed, send, _ = await self.run_body([b"\xff", b"", b"abc"])
        self.assertEqual(observed, [{"type": "http.request", "body": b"\xffabc", "more_body": False}])
        send.assert_not_called()

    async def test_oversize_chunked_body_rejects_before_dispatch(self):
        observed, send, receive = await self.run_body([b"abc", b"de", b"ignored"])
        self.assertEqual(observed, [])
        self.assertEqual(send.call_args_list[0].args[0]["status"], 413)
        self.assertIn((b"connection", b"close"), send.call_args_list[0].args[0]["headers"])
        self.assertEqual(receive.await_count, 2)

    async def test_http2_rejection_omits_connection_specific_header(self):
        send = AsyncMock()
        await RequestBodyLimit(AsyncMock(), 1)({"type": "http", "http_version": "2"},
            AsyncMock(return_value={"type": "http.request", "body": b"xx"}), send)
        self.assertNotIn((b"connection", b"close"), send.call_args_list[0].args[0]["headers"])

    async def test_content_length_cannot_bypass_actual_byte_limit(self):
        observed, send, _ = await self.run_body([b"12345"], headers=[(b"content-length", b"1")])
        self.assertEqual(observed, [])
        self.assertEqual(send.call_args_list[0].args[0]["status"], 413)

    async def test_disconnect_does_not_dispatch(self):
        app = AsyncMock()
        send = AsyncMock()
        await RequestBodyLimit(app, 4)({"type": "http"},
            AsyncMock(return_value={"type": "http.disconnect"}), send)
        app.assert_not_called()
        send.assert_not_called()

    async def test_lifespan_passthrough(self):
        app, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
        scope = {"type": "lifespan"}
        await RequestBodyLimit(app, 4)(scope, receive, send)
        app.assert_awaited_once_with(scope, receive, send)
