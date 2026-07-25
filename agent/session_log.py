"""Local, append-only audit trail — every stage of every pass logs one event
here (ingest, ground, remember, plan, publish, feedback, retrain, ...).

This used to be dressed up as an adapter for a sponsor tool ("Guild") with no
API key ever configured and no real integration behind it — a stub pretending
to be a pluggable external service. That framing worked against the
local-first design everywhere else in this repo: the audit trail genuinely is
local, on purpose, not a placeholder waiting to be swapped for a hosted API.
So it's just what it does — a flat JSONL file — with no sponsor-tool
packaging around it.
"""

import json
from datetime import datetime, timezone

from agent import config


def log_session(event: dict) -> None:
    """Append one audit-trail entry. `event` should describe what the loop did
    (e.g. {"event": "ingest", "file": ..., "post_count": ...})."""
    config.ensure_dirs()
    record = {"logged_at": datetime.now(timezone.utc).isoformat(), **event}
    with config.SESSION_LOG_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def read_sessions() -> list[dict]:
    if not config.SESSION_LOG_FILE.exists():
        return []
    return [json.loads(line) for line in config.SESSION_LOG_FILE.read_text().splitlines() if line.strip()]
