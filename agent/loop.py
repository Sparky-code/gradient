"""Orchestrates one pass or a continuous `/loop`:

  watch drop/ -> ingest -> reclassify (policy) -> evolve taxonomy from
    "other" posts -> plan -> ground + recall (both VectorAI DB) -> publish
    cited.md -> log to the local session log -> check Pioneer for a retrain/promote pass ->
    sleep -> repeat

Ingestion cadence is bounded by when export drops land (see ingest.py) — the
autonomy claim is about everything downstream of that running with no manual
intervention, including retraining off real accept/reject feedback and
growing the category taxonomy off real recurring content.

Reclassify, classify, ground, recall, and taxonomy-evolve run as five local
agents dispatched through orchestrator.Coordinator rather than being called
directly, so a sixth local agent can be added later without this module
changing (see orchestrator.py). The KB/memory *write* step below (VectorAI
remember) stays as a direct call — it's href-dedup bookkeeping tied to its
own state file, not an "input in, output out" pipeline stage. Grounding used
to have a matching write step (Senso ingest) — that's gone now that grounding
reads from VectorAI DB's own memory instead of a separate hosted KB; see
ROADMAP.md for the decoupling.
"""

import json
import time

from agent import cancellation, config, exporter, ingest, orchestrator, publisher, session_log, store, taxonomy
from agent.adapters import pioneer, vectorai

_coordinator = orchestrator.build_default_coordinator()


def _processed_files() -> set[str]:
    if not config.PROCESSED_FILE.exists():
        return set()
    return set(json.loads(config.PROCESSED_FILE.read_text()))


def _mark_processed(name: str) -> None:
    processed = _processed_files()
    processed.add(name)
    config.PROCESSED_FILE.write_text(json.dumps(sorted(processed)))


def _remembered_hrefs() -> set[str]:
    if not config.VECTORAI_REMEMBERED_FILE.exists():
        return set()
    return set(json.loads(config.VECTORAI_REMEMBERED_FILE.read_text()))


def _remember_new_posts_in_vectorai(posts: list[dict]) -> int:
    """Store not-yet-seen high-quality posts as episodic-memory points in
    VectorAI DB, one batch embed + upsert call for the whole set (status
    starts "pending"; feedback.py updates it to the real outcome once the
    user decides). Returns how many were newly remembered (regardless of
    success/failure — a failed push still gets recorded so it isn't retried
    every pass)."""
    already = _remembered_hrefs()
    new_posts = [
        p for p in posts
        if p.get("href") and p["href"] not in already and p.get("actionable") != "entertainment_only"
    ]
    if not new_posts:
        return 0

    result = vectorai.remember_posts(new_posts, status="pending")
    session_log.log_session({"event": "vectorai_remember", "post_count": len(new_posts), **result})
    already |= {p["href"] for p in new_posts}
    config.VECTORAI_REMEMBERED_FILE.write_text(json.dumps(sorted(already)))
    return len(new_posts)


def _process_drop_file(drop_file) -> dict:
    """Runs the full ingest -> reclassify -> taxonomy-evolve -> classify ->
    ground -> recall pipeline for one drop file. Raises on any failure
    (including cancellation.Cancelled) — deliberately does not swallow
    anything itself. run_once() is what decides a failure here shouldn't
    take down the rest of the batch; this function's job is just to make
    sure a failure actually IS isolated to this one file (no partial write
    to _mark_processed()/plans.json survives a raise partway through)."""
    posts = ingest.load_posts(drop_file)
    session_log.log_session({"event": "ingest", "file": drop_file.name, "post_count": len(posts)})

    vectorai_remembered = _remember_new_posts_in_vectorai(posts)

    reclassify_run = _coordinator.create_run(
        "policy-reclassifier",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content=posts, content_type="application/json"),
        ])],
    )
    if reclassify_run.status != "completed":
        raise RuntimeError(f"policy-reclassifier run {reclassify_run.run_id} failed: {reclassify_run.error}")
    reclassify_result = reclassify_run.output[0].parts[0].content
    posts, policy_changes = reclassify_result["posts"], reclassify_result["changes"]
    if policy_changes:
        session_log.log_session({
            "event": "policy_reclassify", "file": drop_file.name,
            "policy_version": reclassify_result["policy_version"],
            "suppressed": [
                {"href": c["href"], "category": c["category"], "reasoning": c.get("policy_reasoning")}
                for c in policy_changes
            ],
        })
    # High-confidence suppression is the actual behavior change feedback
    # drives, not just a log line: it drops the post from this pass's plans.
    posts = [
        p for p in posts
        if not (p.get("policy_confidence") == "high" and p.get("should_surface") is False)
    ]

    if cancellation.is_cancelled():
        raise cancellation.Cancelled()

    # Taxonomy evolution runs alongside planning, not instead of it — "other"
    # posts still flow into a normal plan below exactly as before; this just
    # separately checks whether they're becoming a real, distinct category.
    taxonomy.ensure_seeded([p.get("category") for p in posts if p.get("category") and p.get("category") != "other"])
    taxonomy_run = _coordinator.create_run(
        "taxonomy-evolver",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content=posts, content_type="application/json"),
        ])],
    )
    taxonomy_result = (
        taxonomy_run.output[0].parts[0].content if taxonomy_run.status == "completed" else None
    )
    if taxonomy_result:
        session_log.log_session({"event": "taxonomy_evolve", "file": drop_file.name, **taxonomy_result})
    taxonomy_promoted = 1 if taxonomy_result and taxonomy_result.get("promoted") else 0

    if cancellation.is_cancelled():
        raise cancellation.Cancelled()

    # Must run before classify: it's the only stage that sets tags/
    # category_scores onto posts, which planner.build_plans() then copies
    # onto items when it builds them.
    category_map_run = _coordinator.create_run(
        "category-mapper",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content=posts, content_type="application/json"),
        ])],
    )
    if category_map_run.status == "completed":
        posts = category_map_run.output[0].parts[0].content

    if cancellation.is_cancelled():
        raise cancellation.Cancelled()

    # Also must run before classify, same reasoning as category-mapper above:
    # entity_type/entity_fields need to already be on each post by the time
    # planner.build_plans() copies them onto items.
    actionability_run = _coordinator.create_run(
        "actionability-router",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content=posts, content_type="application/json"),
        ])],
    )
    if actionability_run.status == "completed":
        posts = actionability_run.output[0].parts[0].content

    if cancellation.is_cancelled():
        raise cancellation.Cancelled()

    # Runs alongside planning, not instead of it — same reasoning as
    # taxonomy-evolver above: posts actionability.py couldn't type still flow
    # into a normal plan below exactly as before (entity_type stays None);
    # this just separately checks whether they're becoming a real, distinct
    # export type. A newly-promoted type is backfilled starting next pass.
    export_type_run = _coordinator.create_run(
        "export-type-evolver",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content=posts, content_type="application/json"),
        ])],
    )
    export_type_result = (
        export_type_run.output[0].parts[0].content if export_type_run.status == "completed" else None
    )
    if export_type_result:
        session_log.log_session({"event": "export_type_evolve", "file": drop_file.name, **export_type_result})
    export_type_promoted = 1 if export_type_result and export_type_result.get("promoted") else 0

    if cancellation.is_cancelled():
        raise cancellation.Cancelled()

    classify_run = _coordinator.create_run(
        "classifier",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content=posts, content_type="application/json"),
        ])],
    )
    if classify_run.status != "completed":
        raise RuntimeError(f"classifier run {classify_run.run_id} failed: {classify_run.error}")
    classify_result = classify_run.output[0].parts[0].content
    new_plans, low_quality_count = classify_result["plans"], classify_result["low_quality_count"]

    store.merge_plans(new_plans)  # commits new/updated items; locked internally (store.py)

    # episodic recall: has this interest shown up before, and what did the
    # user actually decide about it? One batch embed call for every new
    # plan's interest, not one model load per plan. Excludes each plan's
    # own items so it surfaces prior passes, not itself.
    recall_run = _coordinator.create_run(
        "vectorai-recaller",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content={
                "query_texts": [plan["interest"] for plan in new_plans],
                "exclude_hrefs_by_text": {
                    plan["interest"]: {item["href"] for item in plan["items"]} for plan in new_plans
                },
            }),
        ])],
    )
    memory_by_interest = recall_run.output[0].parts[0].content if recall_run.status == "completed" else {}
    stub_memory = {"recalled": False, "memories": [], "source": "stub", "reason": recall_run.error}

    # Grounding: same batching reasoning as recall above — one embed
    # subprocess call for every new plan's interest, not one model load per
    # plan. Excludes each plan's own items so grounding cites OTHER saved
    # posts, not a plan's items citing themselves.
    ground_run = _coordinator.create_run(
        "vectorai-grounder",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content={
                "query_texts": [plan["interest"] for plan in new_plans],
                "exclude_hrefs_by_text": {
                    plan["interest"]: {item["href"] for item in plan["items"]} for plan in new_plans
                },
            }),
        ])],
    )
    grounding_by_interest = ground_run.output[0].parts[0].content if ground_run.status == "completed" else {}
    stub_grounding = {"grounded": False, "citations": [], "source": "stub", "reason": ground_run.error}
    grounding_by_plan_id = {
        plan["plan_id"]: grounding_by_interest.get(plan["interest"], stub_grounding)
        for plan in new_plans
    }

    # Fast final commit: re-read fresh rather than reusing the `merged` snapshot
    # from before these slow network/subprocess calls — a feedback action (item
    # click, plan submission) landing on a *different* plan during that window
    # would otherwise get silently reverted by an overwrite based on stale state
    # (see store.py's module docstring for the bug this pattern avoids). Only
    # grounding/memory get touched here, never items/status/tags.
    with store.PLANS_LOCK:
        current = store.load_plans()
        for plan in new_plans:
            pid = plan["plan_id"]
            if pid in current:
                current[pid]["grounding"] = grounding_by_plan_id[pid]
                current[pid]["memory"] = memory_by_interest.get(plan["interest"], stub_memory)
        store.save_plans(current)

    session_log.log_session({
        "event": "plan", "file": drop_file.name,
        "plan_count": len(new_plans), "low_quality_filtered": low_quality_count,
        "vectorai_remembered": vectorai_remembered,
    })
    _mark_processed(drop_file.name)

    return {
        "new_plans": len(new_plans),
        "low_quality_count": low_quality_count,
        "taxonomy_promoted": taxonomy_promoted,
        "export_type_promoted": export_type_promoted,
    }


def run_once() -> dict:
    config.ensure_dirs()
    processed = _processed_files()
    new_files = sorted(
        f for f in config.DROP_DIR.glob("*.json") if f.name not in processed
    )

    total_new_plans = 0
    total_low_quality = 0
    total_taxonomy_promoted = 0
    total_export_types_promoted = 0
    failed_files: list[str] = []

    if new_files:
        # One backup point for the whole pass, not per-file — a run can touch
        # plans/cited.md/policy/taxonomy several times before it's done, and
        # "last good state" should mean "before this run started," not some
        # file-by-file midpoint of it.
        store.snapshot(f"before run cycle ({len(new_files)} file(s))")

    for drop_file in new_files:
        if cancellation.is_cancelled():
            break

        try:
            result = _process_drop_file(drop_file)
        except cancellation.Cancelled:
            break  # stops the whole batch, same as before — cancellation isn't a per-file failure
        except Exception as e:  # noqa: BLE001 - deliberate: one bad file must never abort the rest of the batch
            failed_files.append(drop_file.name)
            session_log.log_session({"event": "ingest_failed", "file": drop_file.name, "reason": str(e)})
            continue  # NOT marked processed — retried on the next pass

        total_new_plans += result["new_plans"]
        total_low_quality += result["low_quality_count"]
        total_taxonomy_promoted += result["taxonomy_promoted"]
        total_export_types_promoted += result["export_type_promoted"]

    publisher.render(low_quality_count=total_low_quality)
    exporter.render_all()
    session_log.log_session({
        "event": "publish", "new_files": len(new_files), "new_plans": total_new_plans,
        "failed_files": failed_files,
    })

    retrain_report = pioneer.maybe_retrain()
    if retrain_report:
        session_log.log_session({"event": "retrain", **retrain_report})

    return {
        "new_files": [f.name for f in new_files],
        "new_plans": total_new_plans,
        "low_quality_filtered": total_low_quality,
        "taxonomy_categories_promoted": total_taxonomy_promoted,
        "export_types_promoted": total_export_types_promoted,
        "retrain_report": retrain_report,
        "cancelled": cancellation.is_cancelled(),
        "failed_files": failed_files,
    }


def run_loop(interval_seconds: int = 60) -> None:
    print(f"self-evolving agent loop starting — polling {config.DROP_DIR} every {interval_seconds}s")
    while True:
        result = run_once()
        if result["new_files"]:
            print(f"[{time.strftime('%H:%M:%S')}] processed {result['new_files']} "
                  f"-> {result['new_plans']} plan(s), {result['low_quality_filtered']} filtered")
        if result["retrain_report"]:
            print(f"[{time.strftime('%H:%M:%S')}] retrain pass: {result['retrain_report']}")
        time.sleep(interval_seconds)
