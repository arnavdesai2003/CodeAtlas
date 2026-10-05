"""Start the same Docker image locally or on Render using its assigned port."""
import os
import sys


def server_arguments(environment):
    value = environment.get("PORT", "8000")
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise ValueError("PORT must be an integer from 1 to 65535")
    return [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0",
            "--port", str(int(value)), "--workers", "1", "--no-proxy-headers"]


def main():
    try:
        arguments = server_arguments(os.environ)
    except ValueError:
        print("Invalid PORT configuration", file=sys.stderr)
        return 1
    os.execv(sys.executable, arguments)


if __name__ == "__main__":
    raise SystemExit(main())
