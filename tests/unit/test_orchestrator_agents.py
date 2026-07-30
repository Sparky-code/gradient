"""Unit tests for the category-mapper agent registered in
agent.orchestrator.build_default_coordinator() — tags each high-quality post
and scores it against every known category anchor, degrading gracefully
(never raising) when its dependencies are unavailable/stubbed to fail.
"""

import pytest

from agent import orchestrator, tagger
from agent.adapters import vectorai


@pytest.fixture
def coordinator():
    return orchestrator.build_default_coordinator()


def _post(**overrides):
    post = {
        "href": "https://www.instagram.com/reel/AAA111/",
        "category": "travel and adventure",
        "subcategory": "hiking trails",
        "actionable": "wishlist_place",
        "action": "go hike it",
        "key_facts": [],
    }
    post.update(overrides)
    return post


def _run_category_mapper(coordinator, posts):
    run = coordinator.create_run(
        "category-mapper",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content=posts, content_type="application/json"),
        ])],
    )
    assert run.status == "completed", run.error
    return run.output[0].parts[0].content


def test_tags_and_category_scores_set_on_high_quality_posts(isolated_env, coordinator, monkeypatch):
    monkeypatch.setattr(tagger, "generate_tags", lambda items: [["hiking", "california"] for _ in items])
    monkeypatch.setattr(vectorai, "sync_anchor_embeddings", lambda category_texts: None)
    monkeypatch.setattr(
        vectorai, "top_k_anchors_many",
        lambda texts, **kwargs: [[{"name": "travel and adventure", "score": 0.9}] for _ in texts],
    )

    result = _run_category_mapper(coordinator, [_post()])

    assert result[0]["tags"] == ["hiking", "california"]
    assert result[0]["category_scores"] == [{"name": "travel and adventure", "score": 0.9}]


def test_low_quality_actionable_posts_skipped(isolated_env, coordinator, monkeypatch):
    calls = []
    monkeypatch.setattr(tagger, "generate_tags", lambda items: calls.append(items) or [[] for _ in items])
    monkeypatch.setattr(vectorai, "sync_anchor_embeddings", lambda category_texts: None)
    monkeypatch.setattr(vectorai, "top_k_anchors_many", lambda texts, **kwargs: [[] for _ in texts])

    result = _run_category_mapper(coordinator, [_post(actionable="entertainment_only")])

    assert calls == []  # tagger never even called — nothing was taggable
    assert "tags" not in result[0]
    assert "category_scores" not in result[0]


def test_degrades_gracefully_when_model_unavailable(isolated_env, coordinator, monkeypatch, tmp_path):
    """Force the real "venv/model missing" degrade path both generate_tags()
    and top_k_anchors_many() already have (rather than relying on this test
    env happening to lack the local model), and confirm the handler still
    completes without raising."""
    monkeypatch.setattr(tagger, "VENV_PYTHON", tmp_path / "no-such-python")
    monkeypatch.setattr(vectorai, "VENV_PYTHON", tmp_path / "no-such-python")
    monkeypatch.setattr(vectorai, "sync_anchor_embeddings", lambda category_texts: None)

    result = _run_category_mapper(coordinator, [_post()])

    assert result[0]["tags"] == []
    assert result[0]["category_scores"] == []


def test_empty_input_returns_empty_list(isolated_env, coordinator):
    assert _run_category_mapper(coordinator, []) == []


def _run_actionability_router(coordinator, posts):
    run = coordinator.create_run(
        "actionability-router",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content=posts, content_type="application/json"),
        ])],
    )
    assert run.status == "completed", run.error
    return run.output[0].parts[0].content


def test_actionability_router_annotates_posts(isolated_env, coordinator):
    result = _run_actionability_router(coordinator, [_post(
        category="food and cooking", subcategory="pasta recipe", hashtags=[],
    )])
    assert result[0]["entity_type"] == "recipe"
    assert "ingredients" in result[0]["entity_fields"]


def test_actionability_router_seeds_export_types_registry(isolated_env, coordinator):
    from agent import export_types

    _run_actionability_router(coordinator, [_post()])
    registry = export_types.load_current()
    assert {t["name"] for t in registry["types"]} == {"music", "location", "recipe"}


def test_export_type_evolver_registered_and_runs(isolated_env, coordinator, monkeypatch, tmp_path):
    # No real Docker/model dependency for this registration-level check —
    # export_type_evolver.evolve()'s own unit tests (test_export_type_evolver.py)
    # already cover the clustering/promotion logic in depth.
    monkeypatch.setattr(vectorai, "VENV_PYTHON", tmp_path / "no-such-python")

    run = coordinator.create_run(
        "export-type-evolver",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content=[_post(entity_type=None)], content_type="application/json"),
        ])],
    )
    assert run.status == "completed", run.error
    result = run.output[0].parts[0].content
    assert result["candidates_seen"] == 1
    assert result["promoted"] is None  # a single post never clusters (CLUSTER_MIN_SIZE=3)


def test_profiler_registered_and_aggregates_current_plans(isolated_env, coordinator):
    from tests.helpers import make_item, make_plan, write_plans

    write_plans(make_plan(interest="hiking", items=[make_item(href="a", status="accepted")]))

    run = coordinator.create_run(
        "profiler",
        input=[orchestrator.Message(role="user", parts=[
            orchestrator.MessagePart(content=None, content_type="application/json"),
        ])],
    )
    assert run.status == "completed", run.error
    result = run.output[0].parts[0].content
    assert result["total_items"] == 1
    assert result["categories"][0]["name"] == "hiking"

    from agent import profile
    assert profile.load_current() == result  # recompute() persisted it, not just returned it
