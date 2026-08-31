import hashlib
import hmac
import json
import urllib.request

from app.core.config import settings


payload = {
    "repository": {
        "clone_url": (
            "https://github.com/karpathy/"
            "micrograd.git"
        )
    },
    "ref": "refs/heads/master",
}

body = json.dumps(
    payload,
    separators=(",", ":"),
).encode("utf-8")


signature = (
    "sha256="
    + hmac.new(
        settings.github_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
)


request = urllib.request.Request(
    "http://127.0.0.1:8000/webhooks/github",
    data=body,
    method="POST",
    headers={
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": signature,
    },
)


with urllib.request.urlopen(
    request
) as response:
    print(response.status)

    print(
        response.read().decode()
    )