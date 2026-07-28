"""Captures the user's accept/reject/share/invite decision on a plan.

This is the real feedback signal: a rejected plan means the classifier/planner
got the interest wrong, an accepted/shared/invited plan means it got it right.
Recording it here is what lets Pioneer's retraining loop run on genuine
production signal instead of synthetic labels.

Every function here that touches plans.json holds store.PLANS_LOCK for its
whole read-modify-write — see store.py's module docstring for the bug this
fixes (a stale full-plans snapshot held across a slow reevaluation silently
reverting other plans' concurrent changes).
"""

from agent import exporter, publisher, reevaluator, session_log, store
from agent.adapters import pioneer, vectorai

DECISION_TO_STATUS = {
    "accept": "accepted",
    "reject": "rejected",
    "share": "shared",
    "invite": "invited",
}

ITEM_DECISION_TO_STATUS = {
    "accept": "accepted",
    "reject": "rejected",
}


def record(plan_id: str, decision: str) -> dict:
    if decision not in DECISION_TO_STATUS:
        raise ValueError(f"decision must be one of {list(DECISION_TO_STATUS)}, got {decision!r}")

    with store.PLANS_LOCK:
        plans = store.load_plans()
        if plan_id not in plans:
            raise KeyError(f"no such plan: {plan_id}")
        plan = plans[plan_id]
        plan["status"] = DECISION_TO_STATUS[decision]
        store.save_plans(plans)

    # Per-item exemplars, not just a plan-level rollup — Pioneer's policy synthesis
    # needs actual subcategory/action content to build few-shot examples from.
    for item in plan["items"]:
        pioneer.submit_feedback({
            "plan_id": plan_id,
            "interest": plan["interest"],
            "decision": decision,
            "href": item["href"],
            "subcategory": item["subcategory"],
            "actionable": item["actionable"],
            "action": item["action"],
        })
    session_log.log_session({"event": "feedback", "plan_id": plan_id, "decision": decision,
                        "item_count": len(plan["items"])})

    # closes the memory loop: recall_similar() on a future pass should see
    # what actually happened here, not the "pending" status it started with.
    memory_update = vectorai.update_status(
        [item["href"] for item in plan["items"]], DECISION_TO_STATUS[decision],
    )
    session_log.log_session({"event": "vectorai_update_status", "plan_id": plan_id, **memory_update})

    publisher.render()
    exporter.render_all()

    # Whole-plan actions resolve the plan immediately in this one call — no
    # separate "submit" step exists for this entry point (that's per-item
    # only, see record_item()), so the tag/reassign/new-category pass runs
    # right here, synchronously. CLI callers accept the wait; web callers use
    # record_item()+submit_plan() instead, which run this in a background
    # thread (see webui.py).
    if decision in ("accept", "reject"):
        reevaluator.reevaluate_plan(plan_id)
        plan = store.load_plans().get(plan_id, plan)
    return plan


def record_item(plan_id: str, href: str, decision: str) -> dict:
    """Per-item accept/reject from the web dashboard. Unlike record(), only the
    one item identified by `href` changes status — its own Pioneer/VectorAI
    feedback is sent, and the rest of the plan's items are left untouched."""
    if decision not in ITEM_DECISION_TO_STATUS:
        raise ValueError(f"decision must be one of {list(ITEM_DECISION_TO_STATUS)}, got {decision!r}")

    with store.PLANS_LOCK:
        plans = store.load_plans()
        if plan_id not in plans:
            raise KeyError(f"no such plan: {plan_id}")
        plan = plans[plan_id]

        item = next((i for i in plan["items"] if i["href"] == href), None)
        if item is None:
            raise KeyError(f"no such item in plan {plan_id}: {href}")

        item["status"] = ITEM_DECISION_TO_STATUS[decision]
        plan["status"] = store.rollup_status(plan["items"])
        # A fully-decided plan (every item has a decision — unanimous or not) is
        # ready for the immediate tag/reassign/new-category pass. "mixed" counts:
        # it means every item was decided, just not unanimously — the pass below
        # (reevaluator.py) knows to drop/reassign only the rejected half and
        # keep the accepted half in place. Per-item feedback doesn't run that
        # pass automatically the moment the last item is clicked (it's a real
        # local-model call, not instant) — this just surfaces a Submit button
        # (see webui.py); submit_plan() below is what actually runs it.
        plan["ready_to_submit"] = plan["status"] in ("accepted", "rejected", "mixed")
        store.save_plans(plans)

    pioneer.submit_feedback({
        "plan_id": plan_id,
        "interest": plan["interest"],
        "decision": decision,
        "href": item["href"],
        "subcategory": item["subcategory"],
        "actionable": item["actionable"],
        "action": item["action"],
    })
    session_log.log_session({"event": "item_feedback", "plan_id": plan_id, "href": href, "decision": decision})

    memory_update = vectorai.update_status([href], item["status"])
    session_log.log_session({"event": "vectorai_update_status", "plan_id": plan_id, **memory_update})

    publisher.render()
    exporter.render_all()
    return plan


def submit_plan(plan_id: str) -> dict:
    """What the web dashboard's "Submit" button (record_item()'s
    ready_to_submit flag) actually calls — the real, potentially-slow
    tag/reassign/new-category pass, run explicitly rather than silently
    firing the moment the last item gets clicked. Raises KeyError if the
    plan isn't ready (defensive — the button shouldn't be clickable
    otherwise, but a stale page could still try). reevaluator.py clears
    ready_to_submit itself as part of its own locked final commit — no
    separate follow-up read-modify-write here, since that would just be
    another race window on top of the one this whole design avoids."""
    with store.PLANS_LOCK:
        plans = store.load_plans()
        if plan_id not in plans:
            raise KeyError(f"no such plan: {plan_id}")
        if not plans[plan_id].get("ready_to_submit"):
            raise ValueError(f"plan {plan_id} is not fully resolved yet")

    return reevaluator.reevaluate_plan(plan_id)
