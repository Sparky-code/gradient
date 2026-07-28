"""Renders type-specific structured export artifacts from current plan state
— music/location/recipe items (plus any later-promoted emergent type) routed
by agent/actionability.py into local files a person imports by hand (no
Spotify/Google Maps API calls, per the project plan's explicit scope).

Mirrors agent/publisher.py's contract exactly: manifest.json is the source
of truth, regenerated wholesale on every render_all() call from
store.load_plans() — the .csv/.md files are a derived view of it, same
"always regenerate, never read back" discipline as cited.md. Called directly
from the same sites publisher.render() is (loop.py, feedback.py,
reevaluator.py), not registered in orchestrator.py — writing a derived file
isn't itself a pipeline capability, the same reasoning that already keeps
publisher.render() out of the Coordinator.

Iterates agent.export_types's registry rather than a hardcoded music/
location/recipe branch set, so a newly-promoted emergent type gets a working
(generic) export immediately, without this module needing a code change.
"""

import csv
import json
from datetime import datetime, timezone

from agent import config, export_types, store

EXPORTS_DIR = config.STATE_DIR / "exports"
EXPORTS_MANIFEST_FILE = EXPORTS_DIR / "manifest.json"
PLAYLIST_CSV = EXPORTS_DIR / "playlist.csv"
PLACES_CSV = EXPORTS_DIR / "places.csv"
RECIPES_DIR = EXPORTS_DIR / "recipes"
SHOPPING_LIST_MD = EXPORTS_DIR / "shopping_list.md"


def _eligible_items(plans: dict[str, dict]) -> list[dict]:
    """Excludes rejected items — exports are meant to be acted on, unlike
    cited.md which still lists rejected items (greyed) for transparency."""
    return [
        {**item, "plan_interest": plan["interest"], "plan_id": plan["plan_id"]}
        for plan in plans.values()
        for item in plan["items"]
        if item.get("status") != "rejected" and item.get("entity_type")
    ]


def _slugify(name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    return slug or "export"


def _render_music(items: list[dict]) -> int:
    with PLAYLIST_CSV.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["artist", "track", "href", "plan_interest"])
        for item in items:
            tracks = item.get("entity_fields", {}).get("tracks") or [{"artist": None, "track": ""}]
            for track in tracks:
                writer.writerow([track.get("artist"), track.get("track"), item["href"], item["plan_interest"]])
    return len(items)


def _render_location(items: list[dict]) -> int:
    with PLACES_CSV.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "address_or_coords", "href", "plan_interest"])
        for item in items:
            fields = item.get("entity_fields", {})
            writer.writerow([fields.get("name"), fields.get("address_or_coords"), item["href"], item["plan_interest"]])
    return len(items)


def _render_recipe(items: list[dict]) -> int:
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    for old in RECIPES_DIR.glob("*.md"):
        old.unlink()

    shopping: dict[str, str] = {}
    for item in items:
        fields = item.get("entity_fields", {})
        name = fields.get("name") or "untitled recipe"
        lines = [f"# {name}", "", f"Source: <{item['href']}>", "", "## Ingredients", ""]
        for ingredient in fields.get("ingredients") or []:
            lines.append(f"- [ ] {ingredient}")
            shopping.setdefault(ingredient.lower(), ingredient)
        lines += ["", "## Instructions", "", item.get("action") or ""]
        (RECIPES_DIR / f"{_slugify(name)}.md").write_text("\n".join(lines))

    SHOPPING_LIST_MD.write_text(
        "# Shopping list\n\n" + "\n".join(f"- [ ] {ingredient}" for ingredient in shopping.values())
    )
    return len(items)


def _render_generic(type_name: str, items: list[dict]) -> str:
    """Every export type this repo hasn't hand-written a dedicated extractor/
    renderer for yet (any `kind: "emergent"` type — see agent/export_types.py)
    gets this: a plain Markdown checklist from whatever generic context
    agent/actionability.py could attach (key_facts/action/tags). Good enough
    to act on immediately; upgradeable to a bespoke renderer later if a given
    emergent type turns out to recur heavily."""
    filename = f"{_slugify(type_name)}.md"
    lines = [f"# {type_name}", ""]
    for item in items:
        fields = item.get("entity_fields", {})
        lines.append(f"- [ ] {item.get('subcategory') or item['href']}")
        for fact in fields.get("key_facts") or []:
            lines.append(f"  - {fact}")
        if fields.get("action"):
            lines.append(f"  - {fields['action']}")
        lines.append(f"  - source: <{item['href']}>")
    (EXPORTS_DIR / filename).write_text("\n".join(lines))
    return filename


_BUILTIN_RENDERERS = {"music": _render_music, "location": _render_location, "recipe": _render_recipe}


def _builtin_files(name: str) -> list[str]:
    """Relative paths (from EXPORTS_DIR) of whatever render_all() just wrote
    for a builtin type — recipe fans out into one card per recipe plus a
    shopping list, so its file list is dynamic; music/location are always
    exactly one file each, even when empty (the CSV header row still exists)."""
    if name == "music":
        return ["playlist.csv"]
    if name == "location":
        return ["places.csv"]
    if name == "recipe":
        return ["shopping_list.md"] + sorted(f"recipes/{p.name}" for p in RECIPES_DIR.glob("*.md"))
    return []


def render_all() -> dict:
    """Regenerates every export artifact from current plan state. Returns
    {"types": [{"name","kind","count","files"}, ...]} — the same shape
    written to manifest.json, so callers/tests can assert on the return
    value without re-reading the file. `files` are paths relative to
    EXPORTS_DIR, suitable for webui.py's /exports/<path:filename> route."""
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    plans = store.load_plans()
    items = _eligible_items(plans)
    items_by_type: dict[str, list[dict]] = {}
    for item in items:
        items_by_type.setdefault(item["entity_type"], []).append(item)

    registry = export_types.ensure_seeded()
    manifest_types = []
    for export_type in registry.get("types", []):
        name = export_type["name"]
        type_items = items_by_type.get(name, [])
        if export_type["kind"] == "builtin":
            count = _BUILTIN_RENDERERS[name](type_items)
            manifest_types.append(
                {"name": name, "kind": "builtin", "count": count, "files": _builtin_files(name)}
            )
        else:
            generic_file = _render_generic(name, type_items) if type_items else None
            manifest_types.append({
                "name": name, "kind": "emergent", "count": len(type_items),
                "files": [generic_file] if generic_file else [],
            })

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "types": manifest_types,
    }
    EXPORTS_MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))
    return manifest
