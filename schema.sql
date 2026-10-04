CREATE SCHEMA IF NOT EXISTS monitor;

CREATE TABLE IF NOT EXISTS monitor.sites (
    id          bigserial PRIMARY KEY,
    url         text NOT NULL UNIQUE,
    alert_url   text,
    cron        text NOT NULL,
    schedule_id text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS monitor.links (
    id              bigserial PRIMARY KEY,
    site_id         bigint NOT NULL REFERENCES monitor.sites (id) ON DELETE CASCADE,
    url             text NOT NULL,
    last_ok         boolean,
    last_status     int,
    last_checked_at timestamptz,
    UNIQUE (site_id, url)
);

-- status is null when the request never got an HTTP answer, error says why
CREATE TABLE IF NOT EXISTS monitor.checks (
    id         bigserial PRIMARY KEY,
    link_id    bigint NOT NULL REFERENCES monitor.links (id) ON DELETE CASCADE,
    ok         boolean NOT NULL,
    status     int,
    error      text,
    checked_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS checks_link_id ON monitor.checks (link_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS monitor.alerts (
    id         bigserial PRIMARY KEY,
    link_id    bigint NOT NULL REFERENCES monitor.links (id) ON DELETE CASCADE,
    ok         boolean NOT NULL,
    status     int,
    -- null until the webhook job is enqueued, so a failed enqueue is retried later
    webhook_job_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alerts_created_at ON monitor.alerts (created_at DESC);
