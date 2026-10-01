import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("CONDUIT_URL", "http://localhost:8080")
API_KEY = os.environ.get("CONDUIT_API_KEY", "")


def call(path, body=None, timeout=30):
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")
