"""Create a NEW production environment file without printing credentials."""
import argparse
import json
import os
import re
import secrets
from pathlib import Path


def hostname(value):
    name = value.lower()
    labels = name.split(".")
    if (len(name) > 253 or len(labels) < 2 or not re.fullmatch(r"[a-z]{2,63}", labels[-1])
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in labels)
            or name.endswith((".example", ".invalid", ".test", ".localhost"))
            or any(name == reserved or name.endswith("." + reserved)
                   for reserved in ("example.com", "example.net", "example.org"))):
        raise argparse.ArgumentTypeError("Use a public DNS hostname you control, without scheme, path or port")
    return name


def create_environment(domain, destination):
    domain = hostname(domain)
    values = {"CODEATLAS_DOMAIN": domain, **{key: secrets.token_hex(32)
              for key in ("POSTGRES_PASSWORD", "API_KEY", "GITHUB_WEBHOOK_SECRET")}}
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write("".join(f"{key}={value}\n" for key, value in values.items()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True, type=hostname)
    parser.add_argument("--output", type=Path, default=Path(".env.production"))
    args = parser.parse_args(argv)
    try:
        create_environment(args.domain, args.output)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "detail": "Environment creation failed; existing files are never replaced."}))
        return 1
    print(json.dumps({"status": "created", "credentials_printed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
