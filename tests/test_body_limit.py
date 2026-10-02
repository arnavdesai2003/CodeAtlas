"""ASGI body-limit boundaries, without servers or dependencies."""
import unittest
from unittest.mock import AsyncMock

from app.api.body_limit import RequestBodyLimit


class BodyLimitTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(receive.await_count, 2)

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
