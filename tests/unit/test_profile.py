"""Unit tests for agent.profile — pure aggregation (compute()) plus the
read/write wrapper (recompute()/load_current()) against isolated_env.
"""

from agent import profile, store

from tests.helpers import make_item, make_plan, write_plans


def test_compute_empty_plans_returns_zeroed_profile():
    result = profile.compute({})
    assert result["total_items"] == 0
    assert result["categories"] == []
    assert result["tags"] == []
    assert result["tag_cooccurrence"] == []
    assert result["generated_at"]


def test_compute_counts_items_and_status_per_category():
    plans = {
        "plan-cooking": make_plan(plan_id="plan-cooking", interest="cooking", items=[
            make_item(href="a", status="accepted"),
            make_item(href="b", status="rejected"),
            make_item(href="c", status="pending"),
        ]),
    }
    result = profile.compute(plans)
    assert result["total_items"] == 3
    assert len(result["categories"]) == 1
    cat = result["categories"][0]
    assert cat["name"] == "cooking"
    assert cat["count"] == 3
    assert cat["accepted"] == 1
    assert cat["rejected"] == 1
    assert cat["pending"] == 1
    assert cat["accept_rate"] == 0.5


def test_compute_accept_rate_is_none_when_nothing_decided():
    plans = {"p": make_plan(items=[make_item(href="a", status="pending")])}
    result = profile.compute(plans)
    assert result["categories"][0]["accept_rate"] is None


def test_compute_categories_sorted_by_count_descending():
    plans = {
        "small": make_plan(plan_id="small", interest="small category", items=[make_item(href="a")]),
        "big": make_plan(plan_id="big", interest="big category", items=[
            make_item(href="b"), make_item(href="c"), make_item(href="d"),
        ]),
    }
    result = profile.compute(plans)
    assert [c["name"] for c in result["categories"]] == ["big category", "small category"]


def test_compute_tag_frequency_across_items():
    plans = {"p": make_plan(items=[
        make_item(href="a", tags=["hiking", "california"]),
        make_item(href="b", tags=["hiking"]),
    ])}
    result = profile.compute(plans)
    tags_by_name = {t["tag"]: t["count"] for t in result["tags"]}
    assert tags_by_name == {"hiking": 2, "california": 1}


def test_compute_tag_cooccurrence_requires_seen_together_more_than_once():
    plans = {"p": make_plan(items=[
        make_item(href="a", tags=["hiking", "california"]),
        make_item(href="b", tags=["hiking", "california"]),
        make_item(href="c", tags=["cooking", "italian"]),  # only co-occurs once — filtered out
    ])}
    result = profile.compute(plans)
    assert result["tag_cooccurrence"] == [{"tags": ["california", "hiking"], "count": 2}]


def test_compute_ignores_items_with_no_tags():
    plans = {"p": make_plan(items=[make_item(href="a", tags=None)])}
    result = profile.compute(plans)
    assert result["tags"] == []
    assert result["tag_cooccurrence"] == []


def test_recompute_writes_current_file_and_returns_it(isolated_env):
    write_plans(make_plan(items=[make_item(href="a", status="accepted", tags=["hiking"])]))
    result = profile.recompute()
    assert profile.CURRENT_FILE.exists()
    assert profile.load_current() == result
    assert result["total_items"] == 1


def test_load_current_default_when_no_file_yet(isolated_env):
    result = profile.load_current()
    assert result["total_items"] == 0
    assert result["generated_at"] is None
