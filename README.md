# conduit-linkcheck

> **This is a learning lab.**
> I built [Conduit](https://github.com/JustinK33/Conduit), a job queue on top of Postgres, and this repo is me testing my own tool from the outside like a real user would.
> It is not a product, and it is not meant to be the best way to check links.

The idea is simple.
You give it a list of URLs, it turns each one into a Conduit job, and a small Python worker picks the jobs up and checks whether each link works.

## How it fits together

```
urls.txt -> enqueue.py -> Conduit (queue "links") -> worker.py -> complete / fail
```

- `enqueue.py` reads `urls.txt` and creates one `check` job per URL.
- `worker.py` asks Conduit for a job, checks the URL, and reports back.
- `conduit.py` is the few lines of HTTP that both scripts share.

Conduit handles everything in between: storing jobs, handing each one to exactly one worker, retrying failures with backoff, and dead-lettering the ones that never work.

## What you need

- Docker with compose
- Python 3.9 or newer, nothing to `pip install`

## Setup

**1. Make a `.env` with two secrets.**

```
cp .env.example .env
```

Fill in both values in `.env`.
Any random string works, for example the output of `openssl rand -hex 32`.
`.env` is gitignored, so the secrets stay on your machine.

**2. Start Postgres and Conduit.**

```
docker compose up -d
```

This starts Postgres, runs Conduit's migrations, and starts Conduit on `localhost:8080`.
Set `CONDUIT_PORT` if 8080 is already taken.

**3. Load the key into your shell.**

```
set -a; . ./.env; set +a
```

Both scripts read `CONDUIT_API_KEY` from the environment.

## Using it

Start the worker in one terminal:

```
python3 worker.py
```

Queue the links in another:

```
python3 enqueue.py
```

The worker prints one line per check:

```
ok   https://www.python.org 200
bad  https://httpbin.org/status/404 404
bad  https://httpbin.org/status/503 503
fail https://this-domain-does-not-exist.invalid (nodename nor servname provided)
```

To check your own links, edit `urls.txt` or pass a file: `python3 enqueue.py my-links.txt`.

## What happens to each link

| Result | What the worker does | Final state |
| --- | --- | --- |
| Under 400 | Completes the job with the status code | `COMPLETED` |
| 404 and other 4xx | Fails it without a retry, it will not fix itself | `DEAD` |
| 5xx, 429, or a network error | Fails it with a retry, then gives up | `DEAD` after 2 attempts |

See every job and where it ended up:

```
curl -s -H "Authorization: Bearer $CONDUIT_API_KEY" localhost:8080/api/jobs
```

## Things worth trying

- Run two workers at once and watch them split the links without ever doubling up.
- Kill a worker in the middle of a check and watch Conduit hand the job to the other one once the lease runs out.
- Start the worker after `enqueue.py` and see that nothing was lost while no one was listening.

## Cleaning up

```
docker compose down -v
```
