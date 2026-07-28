"""Detects music/location/recipe intent — more specific than InstaGone's
generic `actionable` enum — and reshapes already-extracted fields
(key_facts, caption_clean, title, ocr_text, hashtags) into structured entity
fields for agent/exporter.py.

Heuristic (regex/keyword), not a new local-LLM call: the entity content
itself (track/artist names, place names, ingredient lines) is already
sitting in key_facts/caption_clean/hashtags from InstaGone's own extraction
— the only new judgment here is a coarse type bucket, which matching against
already-meaningful category/subcategory/hashtag strings handles without
another subprocess's latency. See the project plan's rationale for why this
stays deterministic rather than model-based.

Music/location/recipe are the 3 `kind: "builtin"` seed types in
agent/export_types.py — not a closed set. Any `kind: "emergent"` type
promoted later by agent/export_type_evolver.py is matched generically here,
via the categories it was minted from, rather than a hand-written extractor.
"""

import re

_RECIPE_KEYWORDS = {"food", "cooking", "recipe", "meal", "kitchen", "baking", "cuisine"}
_MUSIC_KEYWORDS = {"music", "song", "playlist", "album", "artist", "spotify", "track", "dj", "band"}
_LOCATION_KEYWORDS = {"travel", "adventure", "place", "hike", "trail", "destination", "restaurant", "beach", "park"}

_GPS_RE = re.compile(r"-?\d{1,3}\.\d{3,},\s*-?\d{1,3}\.\d{3,}")
_NUMBERED_TRACK_RE = re.compile(
    r"(?:^|(?<=\s))\d+[.):]\s*(@?[\w.]+)\s*[-&]\s*(.+?)\s*$", re.MULTILINE,
)  # numbered items sometimes run on from the preceding sentence rather than
   # starting a fresh line ("...you like! 1. @artist - Track") — the
   # lookbehind lets a leading digit count as an item boundary either at a
   # real line start or after any whitespace, not only after a newline
_INGREDIENT_RE = re.compile(
    r"^\s*[\d/.]+\s*(?:tablespoons?|tbsp|teaspoons?|tsp|cups?|oz|ounces?|g|grams?|"
    r"lbs?|pounds?|ml|cans?|cloves?|handful)\b"
    r"(?!\s*(?:calories?|protein|carbs?|fat)\b)"  # nutrition-fact lines ("40g Protein") match the
    r".*$",                                        # same quantity+unit shape as a real ingredient line
    re.IGNORECASE | re.MULTILINE,
)


def _haystack(post: dict) -> str:
    return " ".join([
        post.get("category") or "", post.get("subcategory") or "",
        " ".join(post.get("hashtags") or []),
    ]).lower()


def _builtin_entity_type(post: dict) -> str | None:
    haystack = _haystack(post)
    # A restaurant review lands in InstaGone's "food and cooking" category (matching
    # _RECIPE_KEYWORDS below) but is a place to visit, not something to cook — checked first
    # so it doesn't get bucketed as a recipe card with an empty ingredients list. Found via a
    # real end-to-end run against fixtures/demo_export.json (Yakitori Edomasa, Gonpachi).
    if "restaurant" in haystack:
        return "location"
    if any(k in haystack for k in _RECIPE_KEYWORDS):
        return "recipe"
    if any(k in haystack for k in _MUSIC_KEYWORDS):
        return "music"
    if any(k in haystack for k in _LOCATION_KEYWORDS):
        return "location"
    return None


def _emergent_entity_type(post: dict, export_types_registry: dict) -> str | None:
    """Fallback for posts the 3 builtin keyword rules don't cover: matches
    against every promoted emergent type's `categories` list, via the post's
    own `category` and the multi-label `category_scores` category-mapper
    already computed (see orchestrator.py's category-mapper agent) — so a
    post can land in a newly-minted export type even before its own single
    `category` was the one the type was originally minted from."""
    candidate_categories = {post.get("category")} | {
        m.get("name") for m in (post.get("category_scores") or [])
    }
    candidate_categories.discard(None)
    for export_type in export_types_registry.get("types", []):
        if export_type.get("kind") != "emergent":
            continue
        if candidate_categories & set(export_type.get("categories") or []):
            return export_type["name"]
    return None


def detect_entity_type(post: dict, export_types_registry: dict | None = None) -> str | None:
    """Builtin keyword fast path first (deterministic, no dependency on the
    export-type registry even being seeded yet); falls back to matching an
    already-promoted emergent type's categories. Returns None — not a forced
    guess — when neither applies, leaving the post as a candidate for
    agent/export_type_evolver.py rather than silently dropped forever."""
    builtin = _builtin_entity_type(post)
    if builtin:
        return builtin
    if export_types_registry:
        return _emergent_entity_type(post, export_types_registry)
    return None


def extract_music(post: dict) -> dict:
    """{"tracks": [{"artist","track"}, ...]} — one row per matched numbered
    line in caption_clean/title; falls back to one row using
    subcategory/action when no numbered-list pattern is found, so a music
    post never ends up with zero structure to export."""
    text = post.get("caption_clean") or post.get("title") or ""
    rows = [{"artist": a.lstrip("@"), "track": t.strip()} for a, t in _NUMBERED_TRACK_RE.findall(text)]
    if not rows:
        rows = [{"artist": None, "track": post.get("subcategory") or post.get("action") or ""}]
    return {"tracks": rows}


def extract_location(post: dict) -> dict:
    """{"name","address_or_coords"} — name from the first key_fact
    (InstaGone's own extracted place names tend to lead that list), coords
    via a GPS-string regex over caption/ocr_text/key_facts if present."""
    key_facts = post.get("key_facts") or []
    text = " ".join([post.get("caption_clean") or "", post.get("ocr_text") or "", " ".join(key_facts)])
    coords = _GPS_RE.search(text)
    return {
        "name": key_facts[0] if key_facts else post.get("subcategory"),
        "address_or_coords": coords.group(0) if coords else None,
    }


def extract_recipe(post: dict) -> dict:
    """{"name","ingredients"} — ingredients via a per-line regex over
    title/caption_clean (InstaGone's own extraction tends to keep ingredient
    lists line-broken there even when it flattens surrounding prose); name
    from subcategory (InstaGone's own dish/subcategory label is already
    recipe-name-shaped, e.g. 'high-protein frozen burrito recipe')."""
    text = "\n".join([post.get("title") or "", post.get("caption_clean") or ""])
    ingredients = [m.group(0).strip() for m in _INGREDIENT_RE.finditer(text)]
    return {"name": post.get("subcategory") or post.get("title"), "ingredients": ingredients}


_EXTRACTORS = {"music": extract_music, "location": extract_location, "recipe": extract_recipe}


def annotate_posts(posts: list[dict], export_types_registry: dict | None = None) -> list[dict]:
    """Sets post["entity_type"] (str|None) and post["entity_fields"] (dict)
    in place, mutating and returning the same list — mirrors category-mapper's
    contract. Pure/no I/O: never raises, never degrades (nothing external to
    fail). Builtin types get their dedicated extractor; emergent types get
    only the generic context already on the post (key_facts/action/tags) —
    enough for exporter.py's generic renderer, upgradeable later if a given
    emergent type turns out to recur heavily."""
    for post in posts:
        entity_type = detect_entity_type(post, export_types_registry)
        post["entity_type"] = entity_type
        if entity_type in _EXTRACTORS:
            post["entity_fields"] = _EXTRACTORS[entity_type](post)
        elif entity_type:
            post["entity_fields"] = {
                "key_facts": post.get("key_facts") or [],
                "action": post.get("action"),
                "tags": post.get("tags") or [],
            }
        else:
            post["entity_fields"] = {}
    return posts
