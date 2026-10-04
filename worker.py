import collections
import datetime
import html.parser
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import db
from conduit import call

PER_HOST_DELAY = float(os.environ.get("PER_HOST_DELAY", "1"))
MAX_PAGE_BYTES = 2 * 1024 * 1024


class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs += [v for k, v in attrs if k == "href" and v]


def extract_links(page, base_url):
    """Absolute http(s) links on the page, without fragments, in order, each once."""
    parser = _Links()
    parser.feed(page)
    links = []
    for href in parser.hrefs:
        url, _ = urllib.parse.urldefrag(urllib.parse.urljoin(base_url, href.strip()))
        if urllib.parse.urlsplit(url).scheme in ("http", "https") and url not in links:
            links.append(url)
    return links


def classify(status):
    """status is an HTTP code, or None when there was no answer. Returns (ok, retry)."""
    if status is None:
        return False, True
    if status < 400:
        return True, False
    # a 404 will still be a 404 in a minute, a 503 might not be
    return False, status >= 500 or status == 429


def spread_by_host(urls, delay):
    """Pair each url with a start offset in seconds, delay apart for urls on the same host."""
    # ponytail: this only spaces the checks of one crawl, two sites on the same
    # host or a retry can still land together. Needs a real per-host limiter for that.
    seen = collections.Counter()
    out = []
    for url in urls:
        host = urllib.parse.urlsplit(url).hostname
        out.append((url, seen[host] * delay))
        seen[host] += 1
    return out


def fetch(url, limit=0):
    """Returns (status, body, final url after redirects). Raises OSError when no HTTP answer came back."""
    # GET rather than HEAD, plenty of sites answer HEAD with 403 or 405
    req = urllib.request.Request(url, headers={"User-Agent": "conduit-linkcheck"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, resp.read(limit).decode(charset, "replace") if limit else "", resp.url
    except urllib.error.HTTPError as e:
        return e.code, "", e.url


def reason(e):
    """The readable part of a network error, without the [Errno 8] prefix."""
    e = getattr(e, "reason", e)
    return getattr(e, "strerror", None) or str(e)


def enqueue(body):
    status, resp = call("/api/jobs", body)
    # a repeated idempotency key also answers 201, with the id of the first job
    if status != 201:
        raise RuntimeError(f"enqueue {body['task']['name']}: {status} {resp}")
    return resp["id"]


def crawl(conn, job, token):
    site = conn.execute("SELECT * FROM monitor.sites WHERE id = %s", (job["task"]["payload"]["site_id"],)).fetchone()
    if site is None:
        return complete(job, token, "site deleted")

    try:
        status, page, page_url = fetch(site["url"], MAX_PAGE_BYTES)
        error = f"http {status}"
    except OSError as e:
        status, error = None, reason(e)
    ok, retry = classify(status)
    if not ok:
        print(f"crawl {site['url']} failed ({error})")
        return fail(job, token, f"fetch {site['url']}: {error}", retry)

    # relative links resolve against where the redirects ended, not the registered url
    urls = extract_links(page, page_url)
    # links that left the page stop being checked, and their stale state goes with them
    conn.execute("DELETE FROM monitor.links WHERE site_id = %s AND url <> ALL(%s::text[])", (site["id"], urls))
    now = datetime.datetime.now(datetime.timezone.utc)
    for url, offset in spread_by_host(urls, PER_HOST_DELAY):
        link_id = conn.execute(
            "INSERT INTO monitor.links (site_id, url) VALUES (%s, %s)"
            " ON CONFLICT (site_id, url) DO UPDATE SET url = EXCLUDED.url RETURNING id",
            (site["id"], url),
        ).fetchone()["id"]
        enqueue({
            # keyed on the crawl job, so a crawl that is re-run after a crash queues nothing twice
            "idempotency_key": f"check:{link_id}:{job['id']}",
            "scheduled_at": (now + datetime.timedelta(seconds=offset)).isoformat(),
            "task": {"name": "check", "queue": "monitor", "max_retries": 3, "payload": {"link_id": link_id}},
        })
    print(f"crawl {site['url']} {len(urls)} links")
    return complete(job, token, f"{len(urls)} links")


def check(conn, job, token):
    link = conn.execute(
        "SELECT l.id, l.url, s.url AS site_url, s.alert_url"
        " FROM monitor.links l JOIN monitor.sites s ON s.id = l.site_id WHERE l.id = %s",
        (job["task"]["payload"]["link_id"],),
    ).fetchone()
    if link is None:
        return complete(job, token, "link deleted")

    try:
        status, _, _ = fetch(link["url"])
        error = None if status < 400 else f"http {status}"
    except OSError as e:
        status, error = None, reason(e)
    ok, retry = classify(status)

    # one flaky 503 should not alert, so only the last attempt is recorded
    if retry and job["attempt"] < job["task"]["max_retries"]:
        print(f"retry {link['url']} ({error}), attempt {job['attempt']} of {job['task']['max_retries']}")
        return fail(job, token, error, retry=True)

    print(f"{'ok' if ok else 'bad':<4} {link['url']} {status or f'({error})'}")
    if db.record_check(conn, link["id"], ok, status, error) is not None:
        print(f"alert {link['url']} is {'back up' if ok else 'broken'}")
    send_alerts(conn, link)
    if ok:
        return complete(job, token, str(status))
    return fail(job, token, error, retry=False)


def send_alerts(conn, link):
    if not link["alert_url"]:
        return
    # every unsent alert of the link, not just a new one, so an alert whose enqueue
    # failed or whose worker died goes out on the retry or the next check
    pending = conn.execute(
        "SELECT id, ok, status FROM monitor.alerts WHERE link_id = %s AND webhook_job_id IS NULL ORDER BY id",
        (link["id"],),
    ).fetchall()
    for a in pending:
        job_id = enqueue({
            "idempotency_key": f"alert:{a['id']}",
            "task": {
                # webhook is built into Conduit and runs in its own pool on queue default
                "name": "webhook",
                "queue": "default",
                "max_retries": 5,
                "metadata": {"url": link["alert_url"]},
                "payload": {"site": link["site_url"], "link": link["url"], "ok": a["ok"], "status": a["status"]},
            },
        })
        conn.execute("UPDATE monitor.alerts SET webhook_job_id = %s WHERE id = %s", (job_id, a["id"]))


def complete(job, token, note):
    report(job, "complete", {"lease_token": token, "metadata": {"result": note}})


def fail(job, token, error, retry):
    report(job, "fail", {"lease_token": token, "error": error, "retry": retry})


def report(job, verb, body):
    status, resp = call(f"/api/jobs/{job['id']}/{verb}", body)
    if status != 200:
        # usually lease_lost: the job took longer than its lease and is someone else's now
        print(f"{verb} {job['id']}: {status} {resp}")


HANDLERS = {"crawl": crawl, "check": check}


def main():
    conn = db.connect()
    db.apply_schema(conn)
    print("waiting for jobs")
    while True:
        try:
            status, body = call(
                "/api/jobs/claim",
                {"queues": ["monitor"], "names": list(HANDLERS), "wait_seconds": 20},
                timeout=40,
            )
        except OSError as e:
            print(f"claim failed: {e}")
            time.sleep(5)
            continue
        if status == 204:
            continue
        if status != 200:
            print(f"claim failed: {status} {body}")
            time.sleep(5)
            continue

        job, token = body["job"], body["lease_token"]
        if conn.closed:
            conn = db.connect()
        try:
            HANDLERS[job["task"]["name"]](conn, job, token)
        except Exception as e:
            # the job goes back to Conduit with a retry instead of taking the worker down
            print(f"error {job['task']['name']} {job['id']}: {e!r}")
            try:
                fail(job, token, repr(e), retry=True)
            except OSError:
                pass  # Conduit is unreachable, the lease runs out and the job is handed out again


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
