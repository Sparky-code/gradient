"""Versioned classification policy — the actual artifact Pioneer's retrain
step produces and auto-promotes. This is what makes "self-evolving" real
instead of a logged number: reclassify.py injects `exemplars` as few-shot
examples into the next classification pass, so accept/reject feedback
visibly changes future category assignments.
"""

import json
from datetime import datetime, timezone

from agent import config

POLICY_DIR = config.STATE_DIR / "policy"
CURRENT_FILE = POLICY_DIR / "current.json"


def load_current() -> dict:
    if not CURRENT_FILE.exists():
        return {"version": 0, "exemplars": []}
    return json.loads(CURRENT_FILE.read_text())


def promote(exemplars: list[dict]) -> dict:
    """Write a new policy version and promote it as current — no human
    approval step, matching the hackathon's autonomy requirement."""
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    next_version = load_current()["version"] + 1
    policy = {
        "version": next_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "exemplars": exemplars,
    }
    (POLICY_DIR / f"v{next_version}.json").write_text(json.dumps(policy, indent=2))
    CURRENT_FILE.write_text(json.dumps(policy, indent=2))
    return policy
