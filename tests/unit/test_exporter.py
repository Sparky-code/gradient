"""Unit tests for agent.exporter.render_all() — regenerates
music/location/recipe (and any emergent type) export artifacts from current
plan state, mirroring publisher.py's "always regenerate" contract.
"""

import json

from agent import export_types, exporter
from tests.helpers import make_item, make_plan, write_plans


def _music_item(**overrides):
    item = make_item(
        href="https://www.instagram.com/reel/MUSIC1/", subcategory="music recommendations",
        actionable="watchlist_media", action="listen to it", status="accepted",
    )
    item["entity_type"] = "music"
    item["entity_fields"] = {"tracks": [{"artist": "kitsebastianpop", "track": "New Internationale"}]}
    item.update(overrides)
    return item


def _location_item(**overrides):
    item = make_item(
        href="https://www.instagram.com/reel/LOC1/", subcategory="hiking trails",
        actionable="wishlist_place", action="go hike it", status="accepted",
    )
    item["entity_type"] = "location"
    item["entity_fields"] = {"name": "Mount Tamalpais State Park", "address_or_coords": None}
    item.update(overrides)
    return item


def _recipe_item(**overrides):
    item = make_item(
        href="https://www.instagram.com/reel/RECIPE1/", subcategory="burrito recipe",
        actionable="how_to", action="make it", status="accepted",
    )
    item["entity_type"] = "recipe"
    item["entity_fields"] = {"name": "Chicken Tikka Masala Burritos", "ingredients": ["3 tablespoons smoked paprika"]}
    item.update(overrides)
    return item


def test_render_all_writes_manifest_and_builtin_files(isolated_env):
    export_types.ensure_seeded()
    write_plans(make_plan(plan_id="plan-a", interest="a", items=[_music_item(), _location_item(), _recipe_item()]))

    manifest = exporter.render_all()

    assert exporter.EXPORTS_MANIFEST_FILE.exists()
    counts = {t["name"]: t["count"] for t in manifest["types"]}
    assert counts == {"music": 1, "location": 1, "recipe": 1}
    assert exporter.PLAYLIST_CSV.exists()
    assert exporter.PLACES_CSV.exists()
    assert exporter.SHOPPING_LIST_MD.exists()
    assert list(exporter.RECIPES_DIR.glob("*.md"))


def test_render_all_playlist_csv_content(isolated_env):
    export_types.ensure_seeded()
    write_plans(make_plan(items=[_music_item()]))
    exporter.render_all()
    rows = exporter.PLAYLIST_CSV.read_text().splitlines()
    assert rows[0] == "artist,track,href,plan_interest"
    assert "kitsebastianpop" in rows[1]
    assert "New Internationale" in rows[1]


def test_render_all_places_csv_content(isolated_env):
    export_types.ensure_seeded()
    write_plans(make_plan(items=[_location_item()]))
    exporter.render_all()
    rows = exporter.PLACES_CSV.read_text().splitlines()
    assert rows[0] == "name,address_or_coords,href,plan_interest"
    assert "Mount Tamalpais State Park" in rows[1]


def test_render_all_recipe_card_and_shopping_list_content(isolated_env):
    export_types.ensure_seeded()
    write_plans(make_plan(items=[_recipe_item()]))
    exporter.render_all()
    recipe_files = list(exporter.RECIPES_DIR.glob("*.md"))
    assert len(recipe_files) == 1
    assert "Chicken Tikka Masala Burritos" in recipe_files[0].read_text()
    assert "3 tablespoons smoked paprika" in exporter.SHOPPING_LIST_MD.read_text()


def test_render_all_excludes_rejected_items(isolated_env):
    export_types.ensure_seeded()
    write_plans(make_plan(items=[_music_item(status="rejected")]))
    manifest = exporter.render_all()
    counts = {t["name"]: t["count"] for t in manifest["types"]}
    assert counts["music"] == 0


def test_render_all_excludes_items_with_no_entity_type(isolated_env):
    export_types.ensure_seeded()
    plain_item = make_item(status="accepted")
    plain_item["entity_type"] = None
    plain_item["entity_fields"] = {}
    write_plans(make_plan(items=[plain_item]))
    manifest = exporter.render_all()
    assert sum(t["count"] for t in manifest["types"]) == 0


def test_render_all_generic_export_for_emergent_type(isolated_env):
    export_types.promote(
        "book recommendations", categories=["books and reading"],
        schema_fields=["key_facts", "action", "tags"], evidence={},
    )
    item = make_item(
        href="https://www.instagram.com/reel/BOOK1/", subcategory="novel recs",
        actionable="how_to", action="read it", status="accepted", key_facts=["Project Hail Mary"],
    )
    item["entity_type"] = "book recommendations"
    item["entity_fields"] = {"key_facts": ["Project Hail Mary"], "action": "read it", "tags": []}
    write_plans(make_plan(items=[item]))

    manifest = exporter.render_all()
    emergent = next(t for t in manifest["types"] if t["name"] == "book recommendations")
    assert emergent["kind"] == "emergent"
    assert emergent["count"] == 1
    assert len(emergent["files"]) == 1
    generic_file = exporter.EXPORTS_DIR / emergent["files"][0]
    assert generic_file.exists()
    assert "Project Hail Mary" in generic_file.read_text()


def test_render_all_seeds_registry_if_not_already_seeded(isolated_env):
    write_plans(make_plan(items=[_music_item()]))
    manifest = exporter.render_all()
    assert {t["name"] for t in manifest["types"]} == {"music", "location", "recipe"}


def test_manifest_file_matches_return_value(isolated_env):
    export_types.ensure_seeded()
    write_plans(make_plan(items=[_music_item()]))
    manifest = exporter.render_all()
    on_disk = json.loads(exporter.EXPORTS_MANIFEST_FILE.read_text())
    assert on_disk["types"] == manifest["types"]
