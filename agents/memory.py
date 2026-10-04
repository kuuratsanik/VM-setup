"""Incident memory in Postgres + pgvector. enabled() is False when VMSETUP_DATABASE_URL is unset."""
import json
import os
import sys

import config  # noqa: F401  (loads /etc/vmsetup/secrets.env)
from openai import OpenAI
from redact import redact

EMBED_MODEL = os.environ.get("VMSETUP_EMBED_MODEL", "embed")

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS incidents (
    id BIGSERIAL PRIMARY KEY,
    ts DOUBLE PRECISION NOT NULL,
    alert TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    model TEXT,
    usage JSONB,
    transcript JSONB,
    feedback TEXT CHECK (feedback IN ('good', 'bad')),
    note TEXT,
    embedding vector,
    embed_model TEXT,
    UNIQUE (ts, alert)
);
"""


def enabled():
    return bool(os.environ.get("VMSETUP_DATABASE_URL"))


def connect():
    import psycopg

    return psycopg.connect(os.environ["VMSETUP_DATABASE_URL"], autocommit=True)


def init():
    with connect() as conn:
        conn.execute(SCHEMA)


def _client():
    return OpenAI(base_url=os.environ.get("LITELLM_URL", "http://127.0.0.1:4000"), api_key=os.environ.get("LITELLM_KEY", "none"))


def embed(text):
    result = _client().embeddings.create(model=EMBED_MODEL, input=redact(text)[:8000])
    return result.data[0].embedding


def _vector(values):
    return "[" + ",".join(str(float(v)) for v in values) + "]"


def add(row):
    """Insert an incident (redacted) with its embedding; returns the id or None if it already existed."""
    from psycopg.types.json import Jsonb

    text = f"{row['alert']}\n{row.get('detail') or ''}"
    try:
        vec = _vector(embed(text))
    except Exception as exc:  # embeddings must never block incident handling
        print(f"memory: embedding failed: {exc}", file=sys.stderr)
        vec = None
    transcript = [{k: redact(v) if isinstance(v, str) else v for k, v in m.items()} for m in (row.get("transcript") or [])]
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO incidents (ts, alert, outcome, detail, model, usage, transcript, embedding, embed_model) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s) ON CONFLICT (ts, alert) DO NOTHING RETURNING id",
            (row["ts"], row["alert"], row["outcome"], redact(row.get("detail")), row.get("model"),
             Jsonb(row.get("usage") or {}), Jsonb(transcript), vec, EMBED_MODEL if vec else None),
        )
        found = cur.fetchone()
    return found[0] if found else None


def similar(text, limit=3):
    """Nearest past incidents; 'bad' ones are excluded and 'good' ones get a small bonus."""
    vec = _vector(embed(text))
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, alert, outcome, detail, feedback, note, embedding <=> %s::vector AS dist FROM incidents "
            "WHERE embedding IS NOT NULL AND vector_dims(embedding) = vector_dims(%s::vector) "
            "AND feedback IS DISTINCT FROM 'bad' "
            "ORDER BY (embedding <=> %s::vector) - (CASE WHEN feedback = 'good' THEN 0.1 ELSE 0 END) LIMIT %s",
            (vec, vec, vec, limit),
        ).fetchall()
    keys = ("id", "alert", "outcome", "detail", "feedback", "note", "distance")
    return [dict(zip(keys, r)) for r in rows]


def set_feedback(incident_id, label, note=None):
    with connect() as conn:
        cur = conn.execute("UPDATE incidents SET feedback = %s, note = %s WHERE id = %s", (label, note, incident_id))
        return cur.rowcount


def recent(limit=20):
    with connect() as conn:
        rows = conn.execute("SELECT id, ts, alert, outcome, feedback, left(detail, 80) FROM incidents ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    return rows


def reviewed(label="good"):
    """Incidents with human feedback, oldest first; used by training and eval export."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, alert, detail, transcript FROM incidents WHERE feedback = %s AND outcome = 'handled' ORDER BY id", (label,)
        ).fetchall()
    return [{"id": r[0], "alert": r[1], "detail": r[2], "transcript": r[3] or []} for r in rows]


def ts_alert_pairs():
    with connect() as conn:
        return {(r[0], r[1]) for r in conn.execute("SELECT ts, alert FROM incidents").fetchall()}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        init()
        print("memory: schema ready")
    else:
        print(json.dumps({"enabled": enabled()}))
