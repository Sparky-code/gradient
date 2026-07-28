"""Unit tests for agent.export_types — the versioned export-type registry,
seeded with 3 builtin types (music/location/recipe) and open to new
`kind: "emergent"` types minted at runtime, mirroring agent.taxonomy's own
versioned-registry contract.
"""

from agent import export_types


def test_load_current_defaults_when_no_file(isolated_env):
    assert export_types.load_current() == {"version": 0, "types": [], "history": []}


def test_ensure_seeded_seeds_three_builtin_types(isolated_env):
    seeded = export_types.ensure_seeded()
    names = {t["name"] for t in seeded["types"]}
    assert names == {"music", "location", "recipe"}
    assert all(t["kind"] == "builtin" for t in seeded["types"])
    assert seeded["version"] == 0


def test_ensure_seeded_is_idempotent(isolated_env):
    export_types.ensure_seeded()
    export_types.promote("book recommendations", categories=["books and reading"],
                          schema_fields=["title", "author"], evidence={"cluster_size": 3})
    reseeded = export_types.ensure_seeded()
    assert len(reseeded["types"]) == 4  # promote() isn't undone by a second ensure_seeded() call


def test_builtin_names():
    assert export_types.builtin_names() == {"music", "location", "recipe"}


def test_promote_adds_emergent_type(isolated_env):
    export_types.ensure_seeded()
    registry = export_types.promote(
        "book recommendations", categories=["books and reading"],
        schema_fields=["title", "author"], evidence={"cluster_size": 3, "cluster_hrefs": ["a", "b", "c"]},
    )
    emergent = [t for t in registry["types"] if t["name"] == "book recommendations"][0]
    assert emergent["kind"] == "emergent"
    assert emergent["categories"] == ["books and reading"]
    assert emergent["schema_fields"] == ["title", "author"]
    assert registry["version"] == 1
    assert registry["history"][-1]["type"] == "book recommendations"
    assert registry["history"][-1]["evidence"]["cluster_size"] == 3


def test_promote_is_idempotent_for_same_type_name(isolated_env):
    export_types.ensure_seeded()
    first = export_types.promote("book recommendations", ["books and reading"], ["title"], {})
    second = export_types.promote("book recommendations", ["books and reading"], ["title"], {})
    assert first == second
    assert second["version"] == 1  # no duplicate version bump on retry


def test_promote_without_prior_seeding_seeds_first(isolated_env):
    registry = export_types.promote("book recommendations", ["books and reading"], ["title"], {})
    names = {t["name"] for t in registry["types"]}
    assert names == {"music", "location", "recipe", "book recommendations"}


def test_promote_writes_versioned_history_file(isolated_env):
    export_types.ensure_seeded()
    export_types.promote("book recommendations", ["books and reading"], ["title"], {})
    assert (export_types.EXPORT_TYPES_DIR / "v1.json").exists()
