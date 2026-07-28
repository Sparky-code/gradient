"""Unit tests for agent.export_type_evolver.evolve() — the same
cluster-then-name-then-promote pipeline agent.taxonomy_evolver.py already
applies to categories, applied one level up to export types. VectorAI DB
calls and the local-LLM namer worker are stubbed; no Docker/model required.
"""

from agent import export_type_evolver, export_types
from agent.adapters import vectorai


def _post(href, category="books and reading", subcategory="novel recs", action="read it", actionable="how_to"):
    return {
        "href": href, "category": category, "subcategory": subcategory,
        "action": action, "actionable": actionable, "entity_type": None,
    }


def test_no_untyped_actionable_posts_returns_zero_candidates(isolated_env):
    posts = [{**_post("a"), "entity_type": "recipe"}]
    result = export_type_evolver.evolve(posts)
    assert result == {"candidates_seen": 0, "cluster_found": False, "promoted": None}


def test_low_quality_actionable_posts_excluded_from_candidates(isolated_env):
    posts = [_post("a", actionable="entertainment_only")]
    result = export_type_evolver.evolve(posts)
    assert result["candidates_seen"] == 0


def test_cluster_below_min_size_not_promoted(isolated_env, monkeypatch):
    monkeypatch.setattr(vectorai, "sync_anchor_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(vectorai, "remember_candidates", lambda *a, **k: {"remembered": 1, "reason": None})
    monkeypatch.setattr(vectorai, "cluster_neighbors_many", lambda *a, **k: {"a": []})

    posts = [_post("a")]
    result = export_type_evolver.evolve(posts)
    assert result == {"candidates_seen": 1, "cluster_found": False, "promoted": None}


def test_cluster_found_but_reuse_matched_skips_promotion(isolated_env, monkeypatch):
    monkeypatch.setattr(vectorai, "sync_anchor_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(vectorai, "remember_candidates", lambda *a, **k: {"remembered": 3, "reason": None})
    monkeypatch.setattr(vectorai, "cluster_neighbors_many", lambda *a, **k: {
        "a": [{"href": "b", "subcategory": "novel recs", "action": "read it", "category": "books and reading"},
              {"href": "c", "subcategory": "novel recs", "action": "read it", "category": "books and reading"}],
    })
    monkeypatch.setattr(vectorai, "nearest_anchor", lambda *a, **k: {"name": "book recommendations", "score": 0.7})

    posts = [_post("a")]
    result = export_type_evolver.evolve(posts)
    assert result["cluster_found"] is True
    assert result["promoted"] is None
    assert result["reuse_matched"] == "book recommendations"
    assert export_types.builtin_names() == {t["name"] for t in export_types.load_current()["types"]}


def test_cluster_ungrounded_skips_promotion(isolated_env, monkeypatch):
    monkeypatch.setattr(vectorai, "sync_anchor_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(vectorai, "remember_candidates", lambda *a, **k: {"remembered": 3, "reason": None})
    monkeypatch.setattr(vectorai, "cluster_neighbors_many", lambda *a, **k: {
        "a": [{"href": "b", "subcategory": "novel recs", "action": "read it", "category": "books and reading"},
              {"href": "c", "subcategory": "novel recs", "action": "read it", "category": "books and reading"}],
    })
    monkeypatch.setattr(vectorai, "nearest_anchor", lambda *a, **k: None)
    monkeypatch.setattr(vectorai, "ground_locally", lambda *a, **k: {"grounded": False, "citations": []})

    posts = [_post("a")]
    result = export_type_evolver.evolve(posts)
    assert result == {"candidates_seen": 1, "cluster_found": True, "promoted": None}


def test_naming_failure_skips_promotion(isolated_env, monkeypatch):
    monkeypatch.setattr(vectorai, "sync_anchor_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(vectorai, "remember_candidates", lambda *a, **k: {"remembered": 3, "reason": None})
    monkeypatch.setattr(vectorai, "cluster_neighbors_many", lambda *a, **k: {
        "a": [{"href": "b", "subcategory": "novel recs", "action": "read it", "category": "books and reading"},
              {"href": "c", "subcategory": "novel recs", "action": "read it", "category": "books and reading"}],
    })
    monkeypatch.setattr(vectorai, "nearest_anchor", lambda *a, **k: None)
    monkeypatch.setattr(vectorai, "ground_locally", lambda *a, **k: {"grounded": True, "citations": ["a citation"]})
    monkeypatch.setattr(export_type_evolver, "_propose_type", lambda *a, **k: {"type": None, "description": None})

    posts = [_post("a")]
    result = export_type_evolver.evolve(posts)
    assert result == {"candidates_seen": 1, "cluster_found": True, "promoted": None}


def test_genuine_cluster_promotes_new_emergent_type(isolated_env, monkeypatch):
    monkeypatch.setattr(vectorai, "sync_anchor_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(vectorai, "remember_candidates", lambda *a, **k: {"remembered": 3, "reason": None})
    monkeypatch.setattr(vectorai, "cluster_neighbors_many", lambda *a, **k: {
        "a": [{"href": "b", "subcategory": "novel recs", "action": "read it", "category": "books and reading"},
              {"href": "c", "subcategory": "novel recs", "action": "read it", "category": "books and reading"}],
    })
    monkeypatch.setattr(vectorai, "nearest_anchor", lambda *a, **k: None)
    monkeypatch.setattr(vectorai, "ground_locally", lambda *a, **k: {"grounded": True, "citations": ["a citation"]})
    monkeypatch.setattr(
        export_type_evolver, "_propose_type",
        lambda *a, **k: {"type": "book recommendations", "description": "Books worth reading"},
    )

    posts = [_post("a")]
    result = export_type_evolver.evolve(posts)

    assert result["promoted"] == "book recommendations"
    assert result["export_types_version"] == 1
    registry = export_types.load_current()
    emergent = next(t for t in registry["types"] if t["name"] == "book recommendations")
    assert emergent["kind"] == "emergent"
    assert emergent["categories"] == ["books and reading"]
