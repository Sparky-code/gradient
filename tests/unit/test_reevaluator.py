"""Unit tests for agent.reevaluator's history tracking (ROADMAP.md §2.2) —
the pure helpers only; reevaluate_plan() itself pulls in VectorAI DB/local
models and is exercised end-to-end elsewhere, not here.
"""

from agent import reevaluator


def test_append_history_creates_list_on_first_event():
    item = {"href": "x"}
    reevaluator._append_history(item, "tagged")
    assert len(item["history"]) == 1
    entry = item["history"][0]
    assert entry["event"] == "tagged"
    assert entry["from_plan"] is None
    assert entry["score"] is None
    assert entry["reason"] is None
    assert entry["at"]  # non-empty ISO timestamp


def test_append_history_appends_never_overwrites():
    item = {"href": "x"}
    reevaluator._append_history(item, "tagged")
    reevaluator._append_history(item, "reassigned", from_plan="cooking", score=0.91, reason="matched 'baking'")
    assert [e["event"] for e in item["history"]] == ["tagged", "reassigned"]
    assert item["history"][1]["from_plan"] == "cooking"
    assert item["history"][1]["score"] == 0.91


def test_strip_transient_drops_status_and_category_but_keeps_history():
    item = {"href": "x", "status": "rejected", "category": "cooking", "tags": ["a"]}
    reevaluator._append_history(item, "reassigned", from_plan="cooking", score=0.8, reason="matched 'baking'")
    stripped = reevaluator._strip_transient(item)
    assert "status" not in stripped
    assert "category" not in stripped
    assert stripped["tags"] == ["a"]
    assert len(stripped["history"]) == 1
    assert stripped["history"][0]["event"] == "reassigned"


def test_upsert_into_carries_history_forward_into_new_plan():
    item = {"href": "x", "status": "rejected", "category": "cooking"}
    reevaluator._append_history(item, "orphaned", from_plan="cooking", reason="no matching category — filed as \"other\"")
    plans = {}
    reevaluator._upsert_into(plans, "other", "other", "2026-01-01T00:00:00+00:00", [item])
    landed = plans["other"]["items"][0]
    assert landed["history"][0]["event"] == "orphaned"
    assert "status" not in landed
