# conduit-linkcheck

> **This is a learning lab.**
> I built [Conduit](https://github.com/JustinK33/Conduit), a job queue on top of Postgres, and this repo is me testing my own tool from the outside like a real user would.
> It is not a product, and it is not meant to be the best way to monitor a website.

It is a small site monitor.
You register a page, and Conduit re-crawls it on a cron.
Each crawl collects the links on the page and fans out one check job per link.
Every result is stored, and when a link flips between working and broken, you get an alert.

## How it fits together

```mermaid
flowchart LR
    you([you]) -->|POST /sites| api[api.py]
    api -->|schedule + first crawl| conduit[(Conduit)]
    conduit -->|crawl / check jobs, queue monitor| worker[worker.py x N]
    worker -->|one check per link| conduit
    worker -->|links, checks, alerts| pg[(Postgres, schema monitor)]
    api -->|reads| pg
    worker -->|webhook job on a flip| conduit
    conduit -->|built in webhook task, queue default| receiver([your alert URL])
```

- `api.py` is a small JSON API on the standard library `http.server`.
  It stores the site and creates a Conduit schedule named `site-<id>` that enqueues a `crawl` job on every cron tick.
- `worker.py` claims `crawl` and `check` jobs from the `monitor` queue.
  A crawl fetches the page, keeps the `http` and `https` links, and enqueues one `check` per link.
  A check fetches one link and records the result.
- `db.py` connects to Postgres and records a check result, deciding in one transaction whether the link flipped.
- `conduit.py` is the few lines of HTTP that talk to Conduit.
- `schema.sql` holds the four tables, which `api.py` and `worker.py` create on startup if they are missing.

Conduit handles everything in between: firing the cron, handing each job to exactly one worker, retrying failures with backoff, dead-lettering the ones that never work, and delivering the alert webhooks.
The monitor keeps its tables in their own `monitor` schema, in the same database as Conduit.

## What you need

- Docker with compose
- Python 3.9 or newer

## Setup

**1. Make a `.env` with two secrets.**

```
cp .env.example .env
```

Fill in `POSTGRES_PASSWORD` and `CONDUIT_API_KEY`, for example with the output of `openssl rand -hex 32`.
Put the same password into `DATABASE_URL`.
`.env` is gitignored, so the secrets stay on your machine.

**2. Start Postgres and Conduit.**

```
docker compose up -d
```

This starts Postgres on `localhost:5432`, runs Conduit's migrations, and starts Conduit on `localhost:8080`.
If either port is taken, set `POSTGRES_PORT` or `CONDUIT_PORT`, and change `DATABASE_URL` or `CONDUIT_URL` to match.

**3. Make a venv and install the one dependency.**

```
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

**4. Load `.env` into your shell.**

```
set -a; . ./.env; set +a
```

Do this in every terminal you start the monitor from.

**5. Start the API and a worker, each in its own terminal.**

```
python api.py
python worker.py
```

The API listens on `localhost:8000`, set `MONITOR_PORT` to move it.

## Using it

Register a site:

```
curl -s -X POST localhost:8000/sites -d '{"url": "https://example.com", "alert_url": "https://hooks.example.com/linkcheck", "cron": "*/15 * * * *"}'
```

Only `url` is required.
`cron` is five fields in UTC and defaults to `0 * * * *`, once an hour.
The site is also crawled right away, so you see results without waiting for the first tick.

The worker prints one line per job:

```
crawl http://localhost:9000/index.html 3 links
ok   http://localhost:9000/good.html 200
bad  http://localhost:9000/missing.html 404
retry http://nope.invalid/ (nodename nor servname provided, or not known), attempt 1 of 3
retry http://nope.invalid/ (nodename nor servname provided, or not known), attempt 2 of 3
bad  http://nope.invalid/ (nodename nor servname provided, or not known)
```

The rest of the API:

```
curl -s localhost:8000/sites                 # every site
curl -s localhost:8000/sites/1               # one site and the current state of each link
curl -s 'localhost:8000/alerts?limit=20'     # newest alerts first
curl -s -X DELETE localhost:8000/sites/1     # stop monitoring, deletes the schedule and the history
```

Errors come back as `{"error": "..."}` with a 4xx or 5xx status.

## What happens to each link

| Result | What the worker does | Recorded |
| --- | --- | --- |
| Under 400 | Completes the job with the status code | As working, straight away |
| 404 and other 4xx | Fails it without a retry, it will not fix itself | As broken, straight away |
| 5xx, 429, or a network error | Fails it with a retry, up to 3 attempts | As broken, only after the last attempt |

Waiting for the last attempt means one flaky 503 does not send an alert.
Links on the same host are spaced `PER_HOST_DELAY` seconds apart within a crawl, 1 by default, so a page with a hundred links does not hit one server a hundred times at once.

## Alerts

The first check of a link only sets a baseline.
After that, a check that disagrees with the previous one is a flip, and it does two things.

1. It inserts a row into `monitor.alerts`, in the same transaction as the check, so a flip is recorded exactly once even with many workers.
2. If the site has an `alert_url`, it enqueues a job for Conduit's built in `webhook` task on queue `default`.

An alert stays marked as unsent until its webhook job is enqueued.
If that enqueue fails, or the worker dies first, the next check of the link sends it.

Each crawl also forgets links that are no longer on the page, so `/sites/<id>` never shows a link you already removed.

Conduit runs that job in its own worker pool and POSTs this to the alert URL, retrying if the receiver is down:

```json
{
  "job_id": "f6703393-8f1e-4ce1-9dea-b155a01e8f13",
  "task_name": "webhook",
  "attempt": 1,
  "payload": {"site": "http://localhost:9000/index.html", "link": "http://localhost:9000/good.html", "ok": true, "status": 200}
}
```

The webhook is sent from inside the Conduit container, so a receiver on your own machine is `http://host.docker.internal:<port>/`, not `localhost`.
The compose file sets `CONDUIT_WEBHOOK_ALLOW_PRIVATE_NETWORKS` so that works, which is fine for a lab and not something to copy into production.

## Things worth trying

- Serve a test page with `python3 -m http.server 9000`, link it to a file that exists, one that does not, and a domain that does not resolve, and register `http://localhost:9000/index.html`.
- Run a tiny receiver that prints POST bodies and use `http://host.docker.internal:<port>/` as the `alert_url`.
- Delete the good file and trigger a crawl without waiting for the cron, straight through Conduit:

  ```
  curl -s -X POST localhost:8080/api/jobs -H "Authorization: Bearer $CONDUIT_API_KEY" \
    -d '{"task": {"name": "crawl", "queue": "monitor", "max_retries": 3, "payload": {"site_id": 1}}}'
  ```

  Then check `/alerts` and your receiver, and put the file back to get the "back up" alert.
- Run two workers at once and watch them split the checks without ever doubling up.
- Kill a worker in the middle of a job and watch Conduit hand the job to the other one once the lease runs out.
- Stop every worker, let the cron fire a few times, and start one again to see that nothing was lost.
- Look at what Conduit itself knows:

  ```
  curl -s -H "Authorization: Bearer $CONDUIT_API_KEY" localhost:8080/api/schedules
  curl -s -H "Authorization: Bearer $CONDUIT_API_KEY" localhost:8080/api/jobs
  ```

## Tests

```
python -m unittest
```

They cover the pure parts of the worker: link extraction, how a status is classified, and the per host spacing.

## What it does not do

- It only checks the links on the page you register, it does not crawl recursively.
- It does not honour `Retry-After`.
- The monitor API has no auth, it listens on `127.0.0.1` only.

## Cleaning up

```
docker compose down -v
```
