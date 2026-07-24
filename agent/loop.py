"""Orchestrates one pass or a continuous `/loop`:

  watch drop/ -> ingest -> reclassify (policy) -> evolve taxonomy from
    "other" posts -> plan -> ground (Senso) + recall (VectorAI DB) -> publish
    cited.md -> log to Guild -> check Pioneer for a retrain/promote pass ->
    sleep -> repeat

Ingestion cadence is bounded by when export drops land (see ingest.py) — the
autonomy claim is about everything downstream of that running with no manual
intervention, including retraining off real accept/reject feedback and
growing the category taxonomy off real recurring content.

Reclassify, classify, ground, recall, and taxonomy-evolve run as five local
agents dispatched through orchestrator.Coordinator rather than being called
directly, so a sixth local agent can be added later without this module
changing (see orchestrator.py). The KB/memory *write* steps below (Senso
ingest, VectorAI remember) stay as direct calls — they're href-dedup
bookkeeping tied to their own state files, not "input in, output out"
pipeline stages.
"""

import json
import time

from agent import cancellation, config, ingest, orchestrator, publisher, store, taxonomy
from agent.adapters import guild, pioneer, senso, vectorai

_coordinator = orchestrator.build_default_coordinator()


def _processed_files() -> set[str]:
    if not config.PROCESSED_FILE.exists():
        return set()
    return set(json.loads(config.PROCESSED_FILE.read_text()))


def _mark_processed(name: str) -> None:
    processed = _processed_files()
    processed.add(name)
    config.PROCESSED_FILE.write_text(json.dumps(sorted(processed)))


def _ingested_hrefs() -> set[str]:
    if not config.SENSO_INGESTED_FILE.exists():
        return set()
    return set(json.loads(config.SENSO_INGESTED_FILE.read_text()))


def _ingest_new_posts_into_senso(posts: list[dict]) -> int:
    """Push each not-yet-seen high-quality post into Senso's KB once. Returns
    how many were newly ingested (regardless of Senso-side success/failure —
    a failed push still gets recorded so we don't retry every pass)."""
    already = _ingested_hrefs()
    new_count = 0
    for post in posts:
        href = post.get("href")
        if not href or href in already or post.get("actionable") == "entertainment_only":
            continue
        result = senso.ingest_post(post)
        guild.log_session({"event": "senso_ingest", "href": href, **result})
        already.add(href)
        new_count += 1
    config.SENSO_INGESTED_FILE.write_text(json.dumps(sorted(already)))
    return new_count


def _remembered_hrefs() -> set[str]:
    if not config.VECTORAI_REMEMBERED_FILE.exists():
        return set()
    return set(json.loads(config.VECTORAI_REMEMBERED_FILE.read_text()))


def _remember_new_posts_in_vectorai(posts: list[dict]) -> int:
    """Store not-yet-seen high-quality posts as episodic-memory points in
    VectorAI DB, one batch embed + upsert call for the whole set (status
    starts "pending"; feedback.py updates it to the real outcome once the
    user decides). Returns how many were newly remembered (regardless of
    success/failure — same retry-avoidance as the Senso ingestion above)."""
    already = _remembered_hrefs()
    new_posts = [
        p for p in posts
        if p.get("href") and p["href"] not in already and p.get("actionable") != "entertainment_only"
    ]
    if not new_posts:
        return 0

    result = vectorai.remember_posts(new_posts, status="pending")
    guild.log_session({"event": "vectorai_remember", "post_count": len(new_posts), **result})
    already |= {p["href"] for p in new_posts}
    config.VECTORAI_REMEMBERED_FILE.write_text(json.dumps(sorted(already)))
    return len(new_posts)


def run_once() -> dict:
    config.ensure_dirs()
    processed = _processed_files()
    new_files = sorted(
        f for f in config.DROP_DIR.glob("*.json") if f.name not in processed
    )

    total_new_plans = 0
    total_low_quality = 0
    total_taxonomy_promoted = 0

    if new_files:
        # One backup point for the whole pass, not per-file — a run can touch
        # plans/cited.md/policy/taxonomy several times before it's done, and
        # "last good state" should mean "before this run started," not some
        # file-by-file midpoint of it.
        store.snapshot(f"before run cycle ({len(new_files)} file(s))")

    for drop_file in new_files:
        if cancellation.is_cancelled():
            break

        posts = ingest.load_posts(drop_file)
        guild.log_session({"event": "ingest", "file": drop_file.name, "post_count": len(posts)})

        senso_ingested = _ingest_new_posts_into_senso(posts)
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
            guild.log_session({
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
            break

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
            guild.log_session({"event": "taxonomy_evolve", "file": drop_file.name, **taxonomy_result})
        total_taxonomy_promoted += 1 if taxonomy_result and taxonomy_result.get("promoted") else 0

        if cancellation.is_cancelled():
            break

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
        total_low_quality += low_quality_count

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

        grounding_by_plan_id = {}
        for plan in new_plans:
            ground_run = _coordinator.create_run(
                "senso-grounder",
                input=[orchestrator.Message(role="user", parts=[
                    orchestrator.MessagePart(content=plan["interest"]),
                ])],
            )
            grounding_by_plan_id[plan["plan_id"]] = (
                ground_run.output[0].parts[0].content if ground_run.status == "completed"
                else {"grounded": False, "citations": [], "source": "stub", "reason": ground_run.error}
            )

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

        total_new_plans += len(new_plans)
        guild.log_session({
            "event": "plan", "file": drop_file.name,
            "plan_count": len(new_plans), "low_quality_filtered": low_quality_count,
            "senso_ingested": senso_ingested, "vectorai_remembered": vectorai_remembered,
        })
        _mark_processed(drop_file.name)

    publisher.render(low_quality_count=total_low_quality)
    guild.log_session({"event": "publish", "new_files": len(new_files), "new_plans": total_new_plans})

    retrain_report = pioneer.maybe_retrain()
    if retrain_report:
        guild.log_session({"event": "retrain", **retrain_report})

    return {
        "new_files": [f.name for f in new_files],
        "new_plans": total_new_plans,
        "low_quality_filtered": total_low_quality,
        "taxonomy_categories_promoted": total_taxonomy_promoted,
        "retrain_report": retrain_report,
        "cancelled": cancellation.is_cancelled(),
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
