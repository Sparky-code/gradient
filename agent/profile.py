"""Self-profiling — aggregates a person's own real interest signal (recurring
categories, accept/reject rates, tag co-occurrence) straight out of
plans.json into data/state/profile/current.json (ROADMAP.md §3). The
lowest-lift of the three "make the sorted knowledge actionable" directions:
a pure read-aggregate over data this pipeline already produces every pass,
no new collection.

Deliberately NOT shaped like policy.py/taxonomy.py/export_types.py: those
model a discrete "did something new get promoted this pass" event, with a
versioned v{n}.json history. A profile has no such event — it's a full
recompute over current plans.json state every time, so there's only ever
one current.json, no history files. It also needs no local-model call (pure
counting), which makes it cheap enough to recompute on every plans.json
mutation rather than just once per run_once() pass — see the "profiler"
orchestrator agent (dispatched once per run_once(), not per drop file, since
this aggregates ALL plans rather than one file's posts) plus the direct
recompute() calls in agent/feedback.py and agent/reevaluator.py, everywhere
plans.json actually changes.
"""

import json
from datetime import datetime, timezone

from agent import config, store

PROFILE_DIR = config.STATE_DIR / "profile"
CURRENT_FILE = PROFILE_DIR / "current.json"

_EMPTY = {"generated_at": None, "total_items": 0, "categories": [], "tags": [], "tag_cooccurrence": []}


def _tag_pairs(tags: list[str]) -> list[tuple[str, str]]:
    uniq = sorted(set(t for t in tags if t))
    return [(uniq[i], uniq[j]) for i in range(len(uniq)) for j in range(i + 1, len(uniq))]


def compute(plans: dict[str, dict]) -> dict:
    """Pure aggregation, no I/O — takes the same shape store.load_plans()
    returns, so it's directly unit-testable without touching disk."""
    category_stats: dict[str, dict] = {}
    tag_counts: dict[str, int] = {}
    cooccurrence: dict[tuple[str, str], int] = {}
    total_items = 0

    for plan in plans.values():
        stats = category_stats.setdefault(
            plan["interest"], {"count": 0, "accepted": 0, "rejected": 0, "pending": 0},
        )
        for item in plan.get("items", []):
            total_items += 1
            stats["count"] += 1
            status = item.get("status", "pending")
            if status in stats:  # item-level status is only ever pending/accepted/rejected
                stats[status] += 1
            tags = item.get("tags") or []
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            for pair in _tag_pairs(tags):
                cooccurrence[pair] = cooccurrence.get(pair, 0) + 1

    categories = []
    for name, stats in category_stats.items():
        decided = stats["accepted"] + stats["rejected"]
        categories.append({
            "name": name, "count": stats["count"], "accepted": stats["accepted"],
            "rejected": stats["rejected"], "pending": stats["pending"],
            # None (not 0.0) when nothing's been decided yet — "no data" and
            # "decided, 0% accepted" are different things a profile shouldn't conflate.
            "accept_rate": round(stats["accepted"] / decided, 2) if decided else None,
        })
    categories.sort(key=lambda c: c["count"], reverse=True)

    tags = sorted(
        ({"tag": t, "count": c} for t, c in tag_counts.items()),
        key=lambda t: t["count"], reverse=True,
    )

    # Only pairs seen together more than once count as "clustering" — a single
    # shared tag between two otherwise-unrelated items is just noise.
    tag_cooccurrence = sorted(
        ({"tags": list(pair), "count": c} for pair, c in cooccurrence.items() if c >= 2),
        key=lambda p: p["count"], reverse=True,
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_items": total_items,
        "categories": categories,
        "tags": tags,
        "tag_cooccurrence": tag_cooccurrence,
    }


def recompute() -> dict:
    """Reads plans.json fresh and writes the recomputed profile. Safe to call
    from anywhere plans.json just changed — cheap pure-Python aggregation,
    no model call, no lock needed (single-writer overwrite of a derived
    artifact, not a read-modify-write against shared state)."""
    result = compute(store.load_plans())
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(json.dumps(result, indent=2))
    return result


def load_current() -> dict:
    if not CURRENT_FILE.exists():
        return dict(_EMPTY)
    return json.loads(CURRENT_FILE.read_text())
