"""Versioned category taxonomy — the direct answer to the audit finding that
InstaGone's anchor category list is completely static (frozen at one account's
export time, structurally unable to grow: anything that doesn't fit gets coerced
to "other").

Same auto-promote-without-human-approval contract as policy.py, for the same
reason: a human gate here would undercut the autonomy claim exactly like it would
for Pioneer. What's different from policy.py is the promotion criterion — a new
category only gets minted once TWO independent signals agree it should exist:

  1. genuine recurrence — VectorAI DB's taxonomy_candidates collection shows a
     real semantic cluster of "other"-bucketed posts, not a one-off (see
     taxonomy_evolver.py)
  2. not already covered — the cluster doesn't score close to any existing
     anchor in taxonomy_anchors (the reuse-weighting the user asked for
     explicitly, so the list grows instead of fragmenting)

Both checks live in agent/taxonomy_evolver.py; this module only owns the
versioned artifact itself.
"""

import json
from datetime import datetime, timezone

from agent import config

TAXONOMY_DIR = config.STATE_DIR / "taxonomy"
CURRENT_FILE = TAXONOMY_DIR / "current.json"

# "other" is the overflow bucket taxonomy_evolver.py mines for candidates — it is
# structurally never a real category. A caller once seeded it in by accident
# (any non-empty category string in a pass's posts, "other" included) and the
# reuse-check then matched a new cluster against an "anchor" built from the
# exact same posts it was clustering (score 0.975 — obviously wrong). Guarded
# here, not just at the one call site, so it can't happen again from a
# different caller.
_NEVER_A_CATEGORY = {"other"}


def load_current() -> dict:
    if not CURRENT_FILE.exists():
        return {"version": 0, "categories": [], "history": []}
    return json.loads(CURRENT_FILE.read_text())


def ensure_seeded(observed_categories: list[str]) -> dict:
    """Bootstrap the taxonomy from whatever categories are actually present in
    this pass's posts — no dependency on InstaGone's own anchor-category file
    (this repo doesn't otherwise read it), and it means the seed reflects
    categories genuinely in use, not a stale account-level snapshot. No-ops
    (returns the existing taxonomy unchanged) once version > 0 — this only
    ever fires once, on the very first pass."""
    current = load_current()
    categories = sorted(set(observed_categories) - _NEVER_A_CATEGORY)
    if current["version"] > 0 or not categories:
        return current
    seeded = {"version": 0, "categories": categories, "history": []}
    CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(json.dumps(seeded, indent=2))
    return seeded


def promote(new_category: str, evidence: dict) -> dict:
    """Add one new category and auto-promote — no human approval, matching
    policy.py's contract. `evidence` is the taxonomy_evolver's record of why
    (cluster size, nearest-anchor score that was checked and cleared, Senso
    grounding citations, which local-model call proposed the name) — kept in
    full in the versioned history, not just a bare category string, so a later
    reviewer can see the actual justification, not just the outcome."""
    TAXONOMY_DIR.mkdir(parents=True, exist_ok=True)
    current = load_current()
    if new_category in _NEVER_A_CATEGORY:
        return current  # the namer worker should never propose this, but never trust it blindly
    if new_category in current["categories"]:
        return current  # already promoted (e.g. a retry) — idempotent, not a new version

    next_version = current["version"] + 1
    taxonomy = {
        "version": next_version,
        "categories": current["categories"] + [new_category],
        "history": current["history"] + [{
            "version": next_version,
            "promoted_at": datetime.now(timezone.utc).isoformat(),
            "category": new_category,
            "evidence": evidence,
        }],
    }
    (TAXONOMY_DIR / f"v{next_version}.json").write_text(json.dumps(taxonomy, indent=2))
    CURRENT_FILE.write_text(json.dumps(taxonomy, indent=2))
    return taxonomy
