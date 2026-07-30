"""Usage-survival trust registry for external learning-plan sources
(ROADMAP.md §3). Decided design, explicitly NOT a factual-reliability
claim — see docs/SOURCE_TRUST.md for the full rationale (what real systems
this mirrors, and why the honesty framing matters).

One-line version: a domain that keeps scoring cleanly across independent
appearances auto-promotes to a "trusted" tier with no human gate — mirroring
this repo's own taxonomy_evolver.py/export_type_evolver.py pattern — but a
single bad showing demotes it immediately, and "trusted" only ever means
"hasn't caused a problem yet," never "verified reliable."

Currently keyed on agent/source_quality.py's automated composite score
across repeated appearances (the only signal that exists today — no
learning-plan feature calls this yet). Designed to fold in real human
accept/reject feedback on a cited source once a learning-plan consumer
exists — see record_pass()'s `feedback` param.
"""

import json
from datetime import datetime, timezone

from agent import config, source_quality

TRUST_DIR = config.STATE_DIR / "source_trust"
CURRENT_FILE = TRUST_DIR / "current.json"

# A domain must clear this on the automated composite to count as a "clean"
# showing — see docs/SOURCE_TRUST.md for why 0.6, not some other cutoff.
CLEAN_THRESHOLD = 0.6

# Matches CLUSTER_MIN_SIZE=3 in taxonomy_evolver.py/export_type_evolver.py —
# same "genuine recurrence, not a one-off" bar, applied here to trust instead
# of category promotion.
PROMOTION_STREAK = 3

def load_current() -> dict:
    if not CURRENT_FILE.exists():
        return {"domains": {}, "history": []}
    return json.loads(CURRENT_FILE.read_text())


def tier_for(url_or_domain: str) -> str:
    """"trusted" | "unverified" | "unknown" (never seen before)."""
    domain = source_quality.domain_of(url_or_domain) or url_or_domain
    record = load_current()["domains"].get(domain)
    return record["tier"] if record else "unknown"


def is_trusted(url_or_domain: str) -> bool:
    return tier_for(url_or_domain) == "trusted"


def _new_record(now: str) -> dict:
    return {
        "tier": "unverified", "clean_streak": 0, "total_passes": 0, "incidents": 0,
        "first_seen": now, "last_seen": None, "promoted_at": None, "demoted_at": None,
    }


def record_pass(scored_results: list[dict], feedback: dict[str, str] | None = None) -> dict:
    """Updates the registry from one batch of agent/source_quality.score_sources()
    output — call this once per external search, after scoring. Dedupes by
    domain within the batch first, so a domain appearing twice in one result
    set (e.g. two pages from the same site) only counts as one pass, not two.

    `feedback` is an optional {url: "accepted"|"rejected"} map from a real
    learning-plan consumer's own accept/reject decisions — once that feature
    exists, an explicit "rejected" always counts as an incident regardless of
    the automated score (a person's decision overrides the composite), and an
    explicit "accepted" always counts as clean. Domains with no feedback
    entry fall back to the automated CLEAN_THRESHOLD check — today, with no
    such consumer wired in yet, `feedback` is always None and every domain
    goes through the automated-only path.

    Never raises: this is bookkeeping over already-computed scores, not a
    network/model call — nothing here has a real failure mode to degrade.
    """
    feedback = feedback or {}
    now = datetime.now(timezone.utc).isoformat()
    registry = load_current()
    domains, history = registry["domains"], registry["history"]

    seen_this_pass: dict[str, bool] = {}  # domain -> is_clean, first result per domain wins
    for result in scored_results:
        domain = source_quality.domain_of(result.get("url") or "")
        if not domain or domain in seen_this_pass:
            continue
        decision = feedback.get(result.get("url"))
        if decision == "rejected":
            is_clean = False
        elif decision == "accepted":
            is_clean = True
        else:
            is_clean = (result.get("quality") or {}).get("overall", 0.0) >= CLEAN_THRESHOLD
        seen_this_pass[domain] = is_clean

    for domain, is_clean in seen_this_pass.items():
        record = domains.setdefault(domain, _new_record(now))
        record["last_seen"] = now
        record["total_passes"] += 1

        if is_clean:
            record["clean_streak"] += 1
            if record["tier"] == "unverified" and record["clean_streak"] >= PROMOTION_STREAK:
                record["tier"] = "trusted"
                record["promoted_at"] = now
                history.append({"domain": domain, "event": "promoted", "at": now,
                                 "clean_streak": record["clean_streak"]})
        else:
            record["incidents"] += 1
            was_trusted = record["tier"] == "trusted"
            record["clean_streak"] = 0
            if was_trusted:
                record["tier"] = "unverified"
                record["demoted_at"] = now
                history.append({"domain": domain, "event": "demoted", "at": now,
                                 "incidents": record["incidents"]})

    TRUST_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(json.dumps(registry, indent=2))
    return registry
