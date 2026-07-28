"""Unit tests for agent.actionability — pure detection/extraction, tested
against real posts read straight from fixtures/demo_export.json so the
extraction assertions reflect actual InstaGone output, not synthetic stand-ins.
"""

import json
from pathlib import Path

import pytest

from agent import actionability

FIXTURE = json.loads((Path(__file__).resolve().parent.parent.parent / "fixtures" / "demo_export.json").read_text())
POSTS = FIXTURE["posts"] if isinstance(FIXTURE, dict) else FIXTURE


def _find(subcategory):
    return next(p for p in POSTS if p.get("subcategory") == subcategory)


def test_detect_entity_type_recipe():
    assert actionability.detect_entity_type(_find("high-protein frozen burrito recipe")) == "recipe"


def test_detect_entity_type_music():
    assert actionability.detect_entity_type(_find("music recommendations")) == "music"


def test_detect_entity_type_location():
    assert actionability.detect_entity_type(_find("hiking trails and outdoor destinations")) == "location"


def test_detect_entity_type_restaurant_review_is_location_not_recipe():
    """Regression: a restaurant review lands in InstaGone's 'food and cooking'
    category (matching the recipe keywords) but is a place to visit, not
    something to cook — found via a real end-to-end run producing an
    empty-ingredients recipe card for 'Yakitori Edomasa'."""
    assert actionability.detect_entity_type(_find("restaurant review")) == "location"


def test_detect_entity_type_restaurant_with_cultural_significance_is_location():
    assert actionability.detect_entity_type(
        _find("Japanese restaurant with cultural and cinematic significance")
    ) == "location"


def test_detect_entity_type_none_for_unrelated_category():
    post = {"category": "personal finance and investing", "subcategory": "budgeting tips", "hashtags": []}
    assert actionability.detect_entity_type(post) is None


def test_extract_music_numbered_tracks_from_real_post():
    tracks = actionability.extract_music(_find("music recommendations"))["tracks"]
    assert {"artist": "kitsebastianpop", "track": "New Internationale"} in tracks
    assert {"artist": "gruppasoyuz", "track": "II"} in tracks


def test_extract_music_falls_back_when_no_numbered_list():
    post = _find("DJ playlist update")
    tracks = actionability.extract_music(post)["tracks"]
    assert tracks == [{"artist": None, "track": "DJ playlist update"}]


def test_extract_location_name_and_coords_from_real_post():
    result = actionability.extract_location(_find("beach and sea cave exploration"))
    assert result["name"] == "Unnamed and unmaintained beach"
    assert result["address_or_coords"] == "37.0005429,-122.1801708"


def test_extract_location_name_without_coords():
    result = actionability.extract_location(_find("hiking trails and outdoor destinations"))
    assert result["name"] == "Mount Tamalpais State Park"
    assert result["address_or_coords"] is None


def test_extract_recipe_ingredients_from_real_burrito_post():
    result = actionability.extract_recipe(_find("high-protein frozen burrito recipe"))
    assert result["name"] == "high-protein frozen burrito recipe"
    joined = " ".join(result["ingredients"]).lower()
    assert "smoked paprika" in joined
    assert any("tsp turmeric" in i.lower() for i in result["ingredients"])


def test_extract_recipe_ingredients_excludes_nutrition_facts():
    """Regression: '40g Protein'/'56g Carbs'/'11g Fat' match the same
    quantity+unit shape as a real ingredient line but aren't ingredients —
    found via a real end-to-end run against fixtures/demo_export.json."""
    result = actionability.extract_recipe(_find("high-protein frozen burrito recipe"))
    lowered = {i.lower() for i in result["ingredients"]}
    assert not lowered & {"40g protein", "56g carbs", "11g fat"}


def test_extract_recipe_ingredients_from_real_apple_cake_post():
    result = actionability.extract_recipe(_find("recipe for invisible apple cake"))
    joined = " ".join(result["ingredients"]).lower()
    assert "baking powder" in joined
    assert any("cinnamon" in i.lower() for i in result["ingredients"])


def test_emergent_type_matched_via_category(monkeypatch):
    registry = {"types": [
        {"name": "book recommendations", "kind": "emergent", "categories": ["books and reading"]},
    ]}
    post = {"category": "books and reading", "subcategory": "novel recs", "hashtags": []}
    assert actionability.detect_entity_type(post, registry) == "book recommendations"


def test_emergent_type_matched_via_category_scores(monkeypatch):
    registry = {"types": [
        {"name": "book recommendations", "kind": "emergent", "categories": ["books and reading"]},
    ]}
    post = {
        "category": "other", "subcategory": "novel recs", "hashtags": [],
        "category_scores": [{"name": "books and reading", "score": 0.7}],
    }
    assert actionability.detect_entity_type(post, registry) == "book recommendations"


def test_builtin_keywords_take_priority_over_emergent_match():
    registry = {"types": [
        {"name": "some emergent type", "kind": "emergent", "categories": ["music and entertainment"]},
    ]}
    post = _find("music recommendations")
    assert actionability.detect_entity_type(post, registry) == "music"


def test_annotate_posts_sets_entity_type_and_fields_for_builtin():
    posts = [dict(_find("high-protein frozen burrito recipe"))]
    actionability.annotate_posts(posts)
    assert posts[0]["entity_type"] == "recipe"
    assert "ingredients" in posts[0]["entity_fields"]


def test_annotate_posts_sets_generic_fields_for_emergent_type():
    registry = {"types": [
        {"name": "book recommendations", "kind": "emergent", "categories": ["books and reading"]},
    ]}
    posts = [{
        "category": "books and reading", "subcategory": "novel recs", "hashtags": [],
        "key_facts": ["Project Hail Mary"], "action": "read it", "tags": ["scifi"],
    }]
    actionability.annotate_posts(posts, registry)
    assert posts[0]["entity_type"] == "book recommendations"
    assert posts[0]["entity_fields"] == {
        "key_facts": ["Project Hail Mary"], "action": "read it", "tags": ["scifi"],
    }


def test_annotate_posts_empty_fields_when_no_type():
    posts = [{"category": "personal finance and investing", "subcategory": "budgeting", "hashtags": []}]
    actionability.annotate_posts(posts)
    assert posts[0]["entity_type"] is None
    assert posts[0]["entity_fields"] == {}
