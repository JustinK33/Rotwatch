# conduit-linkcheck

A tiny link checker built on [Conduit](https://github.com/JustinK33/Conduit).
`enqueue.py` queues one job per line of `urls.txt`, and `worker.py` claims them and checks each URL.

## Run it

```
cp .env.example .env    # then fill in both values, e.g. openssl rand -hex 32
docker compose up -d
set -a; . ./.env; set +a
python3 worker.py &
python3 enqueue.py
```

Working links end up `COMPLETED` with the status code in their metadata.
A 4xx is dead straight away, and a 5xx or a network error is retried once before it is dead.

```
curl -s -H "Authorization: Bearer $CONDUIT_API_KEY" localhost:8080/api/jobs
```

Needs Python 3.9+ and nothing outside the standard library.
