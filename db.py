import os
import pathlib

import psycopg
from psycopg.rows import dict_row

SCHEMA = pathlib.Path(__file__).with_name("schema.sql")


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)


def apply_schema(conn):
    with conn.transaction():
        # the api and every worker run this on startup, and concurrent
        # CREATE ... IF NOT EXISTS can still collide on the catalog
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('monitor.schema'))")
        conn.execute(SCHEMA.read_text())


def record_check(conn, link_id, ok, status, error):
    """Store a final check result. Returns the new alert id if the link flipped, else None."""
    with conn.transaction():
        # the row lock orders two checks of the same link, so a flip is seen exactly once
        link = conn.execute("SELECT last_ok FROM monitor.links WHERE id = %s FOR UPDATE", (link_id,)).fetchone()
        if link is None:
            return None  # the site was deleted while the check was queued
        conn.execute(
            "INSERT INTO monitor.checks (link_id, ok, status, error) VALUES (%s, %s, %s, %s)",
            (link_id, ok, status, error),
        )
        conn.execute(
            "UPDATE monitor.links SET last_ok = %s, last_status = %s, last_checked_at = now() WHERE id = %s",
            (ok, status, link_id),
        )
        # the first check only sets a baseline, there is nothing to flip from
        if link["last_ok"] is None or link["last_ok"] == ok:
            return None
        row = conn.execute(
            "INSERT INTO monitor.alerts (link_id, ok, status) VALUES (%s, %s, %s) RETURNING id",
            (link_id, ok, status),
        ).fetchone()
        return row["id"]
