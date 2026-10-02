"""Bound HTTP body buffering before application parsing or service work."""
from starlette.responses import JSONResponse
import asyncio


class RequestBodyLimit:
    def __init__(self, app, max_bytes: int, timeout_seconds: float = 30.0):
        self.app = app
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        oversized = False
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_bytes:
                        oversized = True
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse(status_code=408, content={
                "detail": "Request body receive timeout."
            })(scope, receive, send)
        if oversized:
            return await JSONResponse(status_code=413, content={
                "detail": "Request body too large."
            })(scope, receive, send)
        payload = bytes(body)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
