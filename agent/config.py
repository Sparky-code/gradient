"""Paths and adapter credentials shared across the agent package."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
DROP_DIR = DATA_DIR / "drop"
STATE_DIR = DATA_DIR / "state"

PLANS_FILE = STATE_DIR / "plans.json"
PROCESSED_FILE = STATE_DIR / "processed_files.json"
TRAINING_QUEUE_FILE = STATE_DIR / "training_queue.jsonl"
VECTORAI_REMEMBERED_FILE = STATE_DIR / "vectorai_remembered.json"
RETRAIN_REPORTS_DIR = STATE_DIR / "retrain_reports"
SESSION_LOG_FILE = STATE_DIR / "session_log.jsonl"
CITED_MD_HASH_FILE = STATE_DIR / "cited_md_last_render_hash.json"

CITED_MD = ROOT / "cited.md"

API_KEYS_FILE = ROOT / "API.md"


def ensure_dirs() -> None:
    DROP_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RETRAIN_REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def load_api_key(name: str) -> str | None:
    """Parse `- <Name>\\n<key>` pairs out of API.md. Returns None if not found —
    callers should fall back to stub behavior rather than crash on a missing key."""
    if not API_KEYS_FILE.exists():
        return None
    lines = [l.strip() for l in API_KEYS_FILE.read_text().splitlines()]
    for i, line in enumerate(lines):
        if line.lower() == f"- {name.lower()}":
            for candidate in lines[i + 1:]:
                if candidate:
                    return candidate
    return None
