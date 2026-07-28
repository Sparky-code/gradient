"""Unit tests for agent.planner.build_plans()'s item-shaping contract — the
one place enrichment fields (source_collection/tags/category_scores/
entity_type/entity_fields) either survive onto a plan item or get silently
dropped.
"""

from agent import planner


def _post(**overrides):
    post = {
        "href": "https://www.instagram.com/reel/AAA111/",
        "category": "travel and adventure",
        "subcategory": "hiking trails",
        "actionable": "wishlist_place",
        "action": "go hike it",
        "key_facts": ["Mount Tamalpais State Park"],
    }
    post.update(overrides)
    return post


def test_source_collection_carried_as_provenance():
    plans, _ = planner.build_plans([
        _post(suggested_collection="Travel", collection_match=True),
    ])
    item = plans[0]["items"][0]
    assert item["source_collection"] == {"name": "Travel", "matched_existing": True}


def test_source_collection_defaults_when_missing():
    plans, _ = planner.build_plans([_post()])
    item = plans[0]["items"][0]
    assert item["source_collection"] == {"name": None, "matched_existing": False}


def test_tags_and_category_scores_pass_through():
    plans, _ = planner.build_plans([
        _post(tags=["hiking", "california"], category_scores=[{"name": "travel and adventure", "score": 0.91}]),
    ])
    item = plans[0]["items"][0]
    assert item["tags"] == ["hiking", "california"]
    assert item["category_scores"] == [{"name": "travel and adventure", "score": 0.91}]


def test_tags_and_category_scores_default_to_empty_list():
    plans, _ = planner.build_plans([_post()])
    item = plans[0]["items"][0]
    assert item["tags"] == []
    assert item["category_scores"] == []


def test_entity_type_and_fields_pass_through():
    plans, _ = planner.build_plans([
        _post(entity_type="location", entity_fields={"name": "Mount Tamalpais State Park", "address_or_coords": None}),
    ])
    item = plans[0]["items"][0]
    assert item["entity_type"] == "location"
    assert item["entity_fields"] == {"name": "Mount Tamalpais State Park", "address_or_coords": None}


def test_entity_type_defaults_to_none_and_fields_to_empty_dict():
    plans, _ = planner.build_plans([_post()])
    item = plans[0]["items"][0]
    assert item["entity_type"] is None
    assert item["entity_fields"] == {}


def test_category_and_ocr_text_still_not_carried_onto_item():
    """Regression guard: category lives only as the plan's `interest`, and
    ocr_text never survives onto an item — this has always been true and
    must stay true, since source_collection is provenance, not a reason to
    start leaking other post-level fields onto items."""
    plans, _ = planner.build_plans([_post(ocr_text="some scanned text")])
    item = plans[0]["items"][0]
    assert "category" not in item
    assert "ocr_text" not in item
    assert plans[0]["interest"] == "travel and adventure"
