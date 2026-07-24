"""Guild adapter — the GOVERNANCE layer (STUB).

No API key configured yet. Guild's real model is session-scoped audit logs;
this stub mirrors that shape as local JSONL so the rest of the loop can
depend on a stable interface, and swapping in the real API later is a
one-function change (append -> POST to Guild's session/workspace endpoint).
"""

import json
from datetime import datetime, timezone

from agent import config


def log_session(event: dict) -> None:
    """Append one audit-trail entry. `event` should describe what the loop did
    (e.g. {"event": "ingest", "file": ..., "post_count": ...})."""
    config.ensure_dirs()
    record = {"logged_at": datetime.now(timezone.utc).isoformat(), **event}
    with config.GUILD_LOG_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def read_sessions() -> list[dict]:
    if not config.GUILD_LOG_FILE.exists():
        return []
    return [json.loads(line) for line in config.GUILD_LOG_FILE.read_text().splitlines() if line.strip()]
