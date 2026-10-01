import time
import urllib.error
import urllib.request

from conduit import call


def check(url):
    # GET rather than HEAD, plenty of sites answer HEAD with 403 or 405
    req = urllib.request.Request(url, headers={"User-Agent": "conduit-linkcheck"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def handle(job, token):
    url = job["task"]["payload"]["url"]
    try:
        code = check(url)
    except OSError as e:
        reason = getattr(e, "reason", e)
        print(f"fail {url} ({reason})")
        return call(f"/api/jobs/{job['id']}/fail", {"lease_token": token, "error": str(reason), "retry": True})

    if code < 400:
        print(f"ok   {url} {code}")
        return call(f"/api/jobs/{job['id']}/complete", {"lease_token": token, "metadata": {"status": str(code)}})

    print(f"bad  {url} {code}")
    # a 404 will still be a 404 in a minute, a 503 might not be
    retry = code >= 500 or code == 429
    return call(f"/api/jobs/{job['id']}/fail", {"lease_token": token, "error": f"http {code}", "retry": retry})


def main():
    print("waiting for links")
    while True:
        status, body = call("/api/jobs/claim", {"queues": ["links"], "names": ["check"], "wait_seconds": 20}, timeout=40)
        if status == 204:
            continue
        if status != 200:
            print(f"claim failed: {status} {body}")
            time.sleep(5)
            continue
        handle(body["job"], body["lease_token"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
