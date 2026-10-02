"""Bound HTTP body buffering before application parsing or service work."""
from starlette.responses import JSONResponse
import asyncio


class RequestBodyLimit:
    def __init__(self, app, max_bytes: int, timeout_seconds: float = 30.0):
        self.app = app
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds

    async def reject(self, scope, receive, send, status, detail):
        headers = {"Connection": "close"} if scope.get("http_version", "1.1") in {"1.0", "1.1"} else {}
        await JSONResponse(status_code=status, content={"detail": detail},
                           headers=headers)(scope, receive, send)

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
            return await self.reject(scope, receive, send, 408, "Request body receive timeout.")
        if oversized:
            return await self.reject(scope, receive, send, 413, "Request body too large.")
        payload = bytes(body)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
