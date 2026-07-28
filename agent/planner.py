"""Turn classified posts into interest "plans" the user can accept, reject,
share, or invite others to.

Quality split reuses InstaGone's existing `actionable` classification instead
of inventing a new one: `entertainment_only` is low-quality/no-action and is
filtered out of plans (but kept in the low-signal count for transparency);
everything else is high-quality/actionable and gets grouped into a plan per
`category`, since that's the natural "interest" grain the classifier already
produces.

`source_collection` carries InstaGone's `suggested_collection`/`collection_match`
through as read-only provenance (the user's own real Instagram Collection
folder, distinct from the AI-assigned `category` above) — it must never be
used as a grouping key or otherwise branched on; it exists purely so the
dashboard/exports can show "you filed this in Travel yourself" alongside the
classifier's own interest grouping.
"""

import hashlib
from datetime import datetime, timezone

LOW_QUALITY_ACTIONABLE = {"entertainment_only"}


def _plan_id(interest: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in interest.lower()).strip("-")
    return f"plan-{slug}" if slug else f"plan-{hashlib.sha1(interest.encode()).hexdigest()[:8]}"


def build_plans(posts: list[dict]) -> tuple[list[dict], int]:
    """Returns (plans, low_quality_count)."""
    high_quality = [p for p in posts if p.get("actionable") not in LOW_QUALITY_ACTIONABLE]
    low_quality_count = len(posts) - len(high_quality)

    grouped: dict[str, list[dict]] = {}
    for post in high_quality:
        interest = post.get("category") or "uncategorized"
        grouped.setdefault(interest, []).append(post)

    now = datetime.now(timezone.utc).isoformat()
    plans = []
    for interest, items in grouped.items():
        plans.append({
            "plan_id": _plan_id(interest),
            "interest": interest,
            "status": "pending",
            "generated_at": now,
            "items": [
                {
                    "href": p.get("href"),
                    "subcategory": p.get("subcategory"),
                    "actionable": p.get("actionable"),
                    "action": p.get("action"),
                    "key_facts": p.get("key_facts") or [],
                    "source_collection": {
                        "name": p.get("suggested_collection"),
                        "matched_existing": bool(p.get("collection_match")),
                    },
                    "tags": p.get("tags") or [],
                    "category_scores": p.get("category_scores") or [],
                    "entity_type": p.get("entity_type"),
                    "entity_fields": p.get("entity_fields") or {},
                }
                for p in items
            ],
        })
    return plans, low_quality_count
