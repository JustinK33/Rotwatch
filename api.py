import http.server
import json
import os
import re
import urllib.parse

import db
from conduit import call

PORT = int(os.environ.get("MONITOR_PORT", "8000"))
MAX_BODY = 64 * 1024


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def parse_url(value, field):
    parts = urllib.parse.urlsplit(value) if isinstance(value, str) else None
    if not parts or parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPError(400, f"{field} must be an http or https url")
    return value


def conduit(path, body=None, method="POST", ok=(200, 201, 204)):
    try:
        status, resp = call(path, body, method=method)
    except OSError as e:
        raise HTTPError(502, f"conduit is unreachable: {getattr(e, 'reason', e)}")
    if status not in ok:
        # pass Conduit's own message through, it already says what was wrong
        message = (resp or {}).get("error", {}).get("message", f"conduit answered {status}")
        raise HTTPError(400 if status == 400 else 502, f"conduit: {message}")
    return resp


def crawl_task(site_id):
    return {"name": "crawl", "queue": "monitor", "max_retries": 3, "payload": {"site_id": site_id}}


def create_site(conn, body):
    unknown = set(body) - {"url", "alert_url", "cron"}
    if unknown:
        raise HTTPError(400, f"unknown fields: {', '.join(sorted(unknown))}")
    url = parse_url(body.get("url"), "url")
    alert_url = None if body.get("alert_url") is None else parse_url(body["alert_url"], "alert_url")
    cron = body.get("cron", "0 * * * *")
    if not isinstance(cron, str):
        raise HTTPError(400, "cron must be a string")

    # the row and the schedule commit together: if Conduit says no, the insert rolls back,
    # and if the commit fails after Conduit said yes, the schedule is deleted again
    sched = None
    try:
        with conn.transaction():
            site = conn.execute(
                "INSERT INTO monitor.sites (url, alert_url, cron) VALUES (%s, %s, %s)"
                " ON CONFLICT (url) DO NOTHING RETURNING *",
                (url, alert_url, cron),
            ).fetchone()
            if site is None:
                raise HTTPError(409, "that url is already monitored")
            sched = conduit("/api/schedules", {"name": f"site-{site['id']}", "cron": cron, "task": crawl_task(site["id"])})
            site = conn.execute(
                "UPDATE monitor.sites SET schedule_id = %s WHERE id = %s RETURNING *", (sched["id"], site["id"])
            ).fetchone()
    except Exception:
        if sched is not None:
            conduit(f"/api/schedules/{sched['id']}", method="DELETE", ok=(204, 404))
        raise

    # crawl now too, the first cron tick could be an hour away
    try:
        site["crawl_job_id"] = conduit("/api/jobs", {"task": crawl_task(site["id"])})["id"]
    except HTTPError:
        site["crawl_job_id"] = None  # the site exists and the cron still crawls it
    return 201, site


def get_site(conn, site_id):
    site = conn.execute("SELECT * FROM monitor.sites WHERE id = %s", (site_id,)).fetchone()
    if site is None:
        raise HTTPError(404, "site not found")
    site["links"] = conn.execute(
        "SELECT id, url, last_ok, last_status, last_checked_at FROM monitor.links WHERE site_id = %s ORDER BY id",
        (site_id,),
    ).fetchall()
    return 200, site


def delete_site(conn, site_id):
    site = conn.execute("SELECT schedule_id FROM monitor.sites WHERE id = %s", (site_id,)).fetchone()
    if site is None:
        raise HTTPError(404, "site not found")
    # schedule first: a row without a schedule is harmless, a schedule without a row crawls nothing forever
    if site["schedule_id"]:
        conduit(f"/api/schedules/{site['schedule_id']}", method="DELETE", ok=(204, 404))
    conn.execute("DELETE FROM monitor.sites WHERE id = %s", (site_id,))
    return 204, None


def list_alerts(conn, query):
    try:
        limit = min(int(query.get("limit", ["50"])[0]), 500)
    except ValueError:
        raise HTTPError(400, "limit must be a number")
    rows = conn.execute(
        "SELECT a.id, a.ok, a.status, a.created_at, l.url AS link, s.url AS site"
        " FROM monitor.alerts a JOIN monitor.links l ON l.id = a.link_id JOIN monitor.sites s ON s.id = l.site_id"
        " ORDER BY a.id DESC LIMIT %s",
        (max(limit, 1),),
    ).fetchall()
    return 200, {"alerts": rows}


def route(conn, method, path, query, body):
    if path == "/sites" and method == "GET":
        return 200, {"sites": conn.execute("SELECT * FROM monitor.sites ORDER BY id").fetchall()}
    if path == "/sites" and method == "POST":
        return create_site(conn, body)
    if path == "/alerts" and method == "GET":
        return list_alerts(conn, query)
    m = re.fullmatch(r"/sites/(\d+)", path)
    if m and method == "GET":
        return get_site(conn, int(m[1]))
    if m and method == "DELETE":
        return delete_site(conn, int(m[1]))
    raise HTTPError(404, "no such route")


class Handler(http.server.BaseHTTPRequestHandler):
    def handle_any(self):
        try:
            body = self.read_body()
            url = urllib.parse.urlsplit(self.path)
            with db.connect() as conn:
                status, resp = route(conn, self.command, url.path, urllib.parse.parse_qs(url.query), body)
        except HTTPError as e:
            status, resp = e.status, {"error": str(e)}
        except Exception as e:
            self.log_error("%r", e)
            status, resp = 500, {"error": "internal error"}
        self.send(status, resp)

    do_GET = do_POST = do_DELETE = handle_any

    def read_body(self):
        if self.command != "POST":
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HTTPError(413, f"body is over {MAX_BODY} bytes")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise HTTPError(400, "body must be json")
        if not isinstance(body, dict):
            raise HTTPError(400, "body must be a json object")
        return body

    def send(self, status, resp):
        data = b"" if resp is None else json.dumps(resp, default=lambda v: v.isoformat(), indent=2).encode() + b"\n"
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    with db.connect() as conn:
        db.apply_schema(conn)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"monitor api on http://localhost:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
