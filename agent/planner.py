"""Turn classified posts into interest "plans" the user can accept, reject,
share, or invite others to.

Quality split reuses InstaGone's existing `actionable` classification instead
of inventing a new one: `entertainment_only` is low-quality/no-action and is
filtered out of plans (but kept in the low-signal count for transparency);
everything else is high-quality/actionable and gets grouped into a plan per
`category`, since that's the natural "interest" grain the classifier already
produces.
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
                }
                for p in items
            ],
        })
    return plans, low_quality_count
