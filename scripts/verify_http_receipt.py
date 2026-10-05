"""Isolated real-socket verification of production body middleware, no stores."""
import http.client
import argparse
import json
import socket
import subprocess
import sys
import time

from fastapi import FastAPI, Request
from app.api.body_limit import RequestBodyLimit


def create_probe_app():
    # Diagnostic fixture only: no production routes, database or models.
    app = FastAPI()
    app.add_middleware(RequestBodyLimit, max_bytes=16, timeout_seconds=.2)
    dispatched = 0

    @app.post("/echo")
    async def echo(request: Request):
        nonlocal dispatched
        dispatched += 1
        return {"bytes": len(await request.body())}

    @app.get("/stats")
    async def stats():
        return {"dispatched": dispatched}

    return app


def request(port, wire):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.sendall(wire)
        response = http.client.HTTPResponse(connection)
        response.begin()
        body = json.loads(response.read())
        if response.status in {408, 413}:
            if response.getheader("Connection", "").lower() != "close" or connection.recv(1) != b"":
                raise RuntimeError("Rejected upload connection was not closed.")
        return response.status, body


def verify_http_receipt():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen([sys.executable, "-B", "-m", "uvicorn",
        "scripts.verify_http_receipt:create_probe_app", "--factory", "--host", "127.0.0.1",
        "--port", str(port), "--no-access-log", "--no-proxy-headers", "--log-level", "warning"])
    head = b"POST /echo HTTP/1.1\r\nHost: localhost\r\n"
    cases = [
        ("partial content-length", head + b"Content-Length: 4\r\n\r\nx", 408),
        ("partial chunked", head + b"Transfer-Encoding: chunked\r\n\r\n1\r\nx\r\n", 408),
        ("oversize content-length", head + b"Content-Length: 17\r\n\r\n" + b"x" * 17, 413),
        ("oversize unfinished upload", head + b"Content-Length: 100\r\n\r\n" + b"x" * 17, 413),
        ("oversize chunked", head + b"Transfer-Encoding: chunked\r\n\r\n11\r\n" + b"x" * 17 + b"\r\n0\r\n\r\n", 413),
        ("exact content-length", head + b"Content-Length: 16\r\n\r\n" + b"x" * 16, 200),
        ("exact chunked", head + b"Transfer-Encoding: chunked\r\n\r\n8\r\n" + b"x" * 8 + b"\r\n8\r\n" + b"y" * 8 + b"\r\n0\r\n\r\n", 200),
    ]
    stats_wire = b"GET /stats HTTP/1.1\r\nHost: localhost\r\n\r\n"
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise RuntimeError("Probe server exited during startup.")
            try:
                status, body = request(port, stats_wire)
                if status != 200 or body != {"dispatched": 0}:
                    raise RuntimeError("Unexpected readiness response.")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
        accepted = 0
        for label, wire, expected in cases:
            start = time.monotonic()
            status, body = request(port, wire)
            if status != expected:
                raise RuntimeError(f"{label}: expected {expected}, received {status}")
            if status == 200:
                accepted += 1
                if body != {"bytes": 16}:
                    raise RuntimeError("Accepted bytes changed.")
            stats_status, stats = request(port, stats_wire)
            if stats_status != 200 or stats != {"dispatched": accepted}:
                raise RuntimeError("Rejected request dispatched work or server became unusable.")
            print(json.dumps({"case": label, "status": status,
                "elapsed_ms": round((time.monotonic() - start) * 1000, 3),
                "dispatched": accepted}), flush=True)
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            connection.sendall(head + b"Content-Length: 16\r\n\r\n" + b"x" * 16)
            first = http.client.HTTPResponse(connection)
            first.begin()
            if first.status != 200 or json.loads(first.read()) != {"bytes": 16}:
                raise RuntimeError("Persistent accepted upload failed.")
            connection.sendall(stats_wire)
            second = http.client.HTTPResponse(connection)
            second.begin()
            if second.status != 200 or json.loads(second.read()) != {"dispatched": accepted + 1}:
                raise RuntimeError("Accepted connection could not be reused.")
        print(json.dumps({"case": "accepted connection reuse", "status": 200}), flush=True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    print(json.dumps({"completed": True, "server_stopped": True, "cases": len(cases) + 1}))


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        verify_http_receipt()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "HTTP verification or owned-server cleanup failed; no success claimed."}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
