"""Runs immediately once a plan is fully decided (every item has a decision —
accepted, rejected, or a mix of both) — the missing half of the feedback loop:
previously, reject only flipped a status badge and queued a Pioneer exemplar
for the *next* scheduled pass; the rejected item itself stayed sitting under
its apparently-wrong category forever. This closes that gap using the exact
clustering/reuse-check/grounding/naming pipeline taxonomy_evolver.py already
proved works for "other"-bucketed posts at initial classification — a
rejected item that doesn't fit anywhere else is the same kind of signal, just
arriving later, from feedback instead of ingest.

Mixed plans (some items accepted, some rejected — no longer just unanimous
accept/reject) are handled by only ever reassigning/orphaning the REJECTED
half; accepted and pending items stay in the plan untouched apart from
getting tagged.

Locking discipline matters here specifically because this pass is slow (a
local-model call per step): only the fast final read-modify-write against
plans.json happens under store.PLANS_LOCK, re-reading fresh at that point
rather than holding a stale snapshot across the whole slow computation — see
store.py's module docstring for the data-loss bug this design avoids.
"""

from agent import cancellation, exporter, planner, publisher, session_log, store, taxonomy, taxonomy_evolver, tagger
from agent.adapters import vectorai

REASSIGN_MIN_SCORE = vectorai.ANCHOR_REUSE_SCORE  # same "is this genuinely the same topic" bar


def _item_text(item: dict) -> str:
    return " ".join([item.get("subcategory") or "", item.get("action") or ""] + item.get("tags", [])).strip()


def _all_items_with_category(plans: dict[str, dict]) -> list[dict]:
    """Flatten every plan's items back into post-shaped dicts (category attached
    from the plan they live under) — the representative-content pool
    sync_anchor_embeddings() needs, same reasoning as taxonomy_evolver.py."""
    return [{**item, "category": plan["interest"]} for plan in plans.values() for item in plan["items"]]


def _strip_transient(item: dict) -> dict:
    """Drop fields that shouldn't carry over when an item moves to a new plan —
    `status`/`category` are relative to where it *was*, not where it's landing
    (a reassigned item starts fresh as "pending" in its new home)."""
    return {k: v for k, v in item.items() if k not in ("category", "status")}


def _upsert_into(plans: dict[str, dict], dest_id: str, interest: str, generated_at: str, items: list[dict]) -> None:
    dest_plan = plans.get(dest_id) or {
        "plan_id": dest_id, "interest": interest, "status": "pending",
        "generated_at": generated_at, "items": [],
    }
    dest_plan["items"].extend(_strip_transient(i) for i in items)
    plans[dest_id] = dest_plan


def reevaluate_plan(plan_id: str) -> dict:
    """Never raises — every real-tool call it uses already degrades to a safe
    no-op on its own failure (VectorAI DB/local model), so a failed
    reassignment or naming attempt just leaves an item as an unclustered
    orphan rather than crashing the plan resolution that triggered this."""
    store.snapshot(f"before reevaluating {plan_id}")

    with store.PLANS_LOCK:
        plans = store.load_plans()
        if plan_id not in plans:
            return {"plan_id": plan_id, "error": "no such plan"}
        plan = plans[plan_id]
        items_snapshot = [dict(item) for item in plan["items"]]
        plan_status, plan_interest, plan_generated_at = plan["status"], plan["interest"], plan["generated_at"]

    # ---- slow work below: no lock held, no live plans dict touched ----

    # Most items already carry tags from ingest-time (category-mapper, see
    # orchestrator.py) — only tag the gaps (items that arrived via a prior
    # reassignment with no tags yet, or where ingest-time tagging degraded to
    # []), instead of re-running the local-LLM tagger on every item every time
    # a plan resolves.
    untagged = [item for item in items_snapshot if not item.get("tags")]
    if untagged:
        tags = tagger.generate_tags(untagged)
        for item, item_tags in zip(untagged, tags):
            item["tags"] = item_tags

    if cancellation.is_cancelled():
        # Nothing committed — the plan stays exactly as it was (still
        # ready_to_submit) so the user can just submit again, rather than
        # landing half-tagged with no reassignment attempted.
        session_log.log_session({"event": "plan_reevaluation_cancelled", "plan_id": plan_id, "stage": "tagging"})
        return {"plan_id": plan_id, "status": plan_status, "cancelled": True}

    result = {
        "plan_id": plan_id, "status": plan_status, "tagged": len(untagged),
        "reassigned": [], "taxonomy_promoted": None, "still_unassigned": 0,
    }

    # Only the rejected half ever gets reassigned/orphaned — accepted and
    # pending items are kept as-is (just tagged). This is what makes mixed
    # plans submittable: "mixed" used to never even show a Submit button.
    rejected_items = [i for i in items_snapshot if i.get("status") == "rejected"]
    keep_items = [i for i in items_snapshot if i.get("status") != "rejected"]

    reassignments: list[tuple[dict, str, float]] = []
    orphans: list[dict] = []
    promoted_category = None

    if rejected_items:
        with store.PLANS_LOCK:
            context_plans = store.load_plans()
        current_taxonomy = taxonomy.load_current()
        vectorai.sync_anchor_embeddings(
            taxonomy_evolver._category_representative_texts(
                _all_items_with_category(context_plans), current_taxonomy["categories"],
            )
        )
        # One batched embed call for every rejected item's reassignment check.
        matches = vectorai.nearest_anchor_many(
            [_item_text(item) for item in rejected_items], min_score=REASSIGN_MIN_SCORE, exclude=plan_interest,
        )
        for item, match in zip(rejected_items, matches):
            if match:
                reassignments.append((item, match["name"], match["score"]))
                result["reassigned"].append({"href": item.get("href"), "to": match["name"], "score": match["score"]})
            else:
                orphans.append(item)

        if cancellation.is_cancelled():
            session_log.log_session({"event": "plan_reevaluation_cancelled", "plan_id": plan_id, "stage": "reassignment"})
            return {"plan_id": plan_id, "status": plan_status, "cancelled": True}

        if orphans:
            # Same clustering/grounding/naming pipeline as "other" posts at ingest
            # time — an orphan that doesn't cluster this round just stays an
            # "other"-tagged item and gets picked up again on a future pass/reject.
            orphan_posts = [{**o, "category": "other"} for o in orphans]
            context_posts = _all_items_with_category(context_plans)
            evolve_result = taxonomy_evolver.evolve(context_posts + orphan_posts)
            promoted_category = evolve_result.get("promoted")
            result["taxonomy_promoted"] = promoted_category
            if not promoted_category:
                result["still_unassigned"] = len(orphans)

    # ---- fast final commit: re-read fresh, apply the computed deltas, save ----

    with store.PLANS_LOCK:
        plans = store.load_plans()
        if plan_id in plans:
            plans[plan_id]["items"] = keep_items
            plans[plan_id]["status"] = store.rollup_status(keep_items) if keep_items else plan_status
            plans[plan_id]["ready_to_submit"] = False

        for item, dest_name, _score in reassignments:
            _upsert_into(plans, planner._plan_id(dest_name), dest_name, plan_generated_at, [item])

        if orphans:
            if promoted_category:
                _upsert_into(plans, planner._plan_id(promoted_category), promoted_category, plan_generated_at, orphans)
            else:
                _upsert_into(plans, planner._plan_id("other"), "other", plan_generated_at, orphans)

        store.save_plans(plans)

    publisher.render()
    exporter.render_all()
    session_log.log_session({"event": "plan_reevaluated", **result})
    return result
