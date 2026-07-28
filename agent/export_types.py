"""Versioned export-type registry — the same self-evolving pattern
agent/taxonomy.py already applies to categories, applied to the *kind* of
actionable content a post can produce a structured local export for.

Music/location/recipe are seed examples, not a closed set: this registry
starts with those three (`kind: "builtin"`, hand-written regex extractors in
agent/actionability.py and hand-written renderers in agent/exporter.py,
appropriate since their entity shapes are well-understood and worth
dedicated rich output) and grows new `kind: "emergent"` types at runtime via
agent/export_type_evolver.py, mirroring taxonomy_evolver.py's own
cluster-then-name-then-promote pipeline. An emergent type only carries
generic fields and renders generically until/unless it earns a bespoke
extractor later.

Same auto-promote-without-human-approval contract as taxonomy.py/policy.py.
"""

import json
from datetime import datetime, timezone

from agent import config

EXPORT_TYPES_DIR = config.STATE_DIR / "export_types"
CURRENT_FILE = EXPORT_TYPES_DIR / "current.json"

_BUILTIN_TYPES = [
    {"name": "music", "kind": "builtin", "schema_fields": ["artist", "track"], "categories": []},
    {"name": "location", "kind": "builtin", "schema_fields": ["name", "address_or_coords"], "categories": []},
    {"name": "recipe", "kind": "builtin", "schema_fields": ["name", "ingredients"], "categories": []},
]


def load_current() -> dict:
    if not CURRENT_FILE.exists():
        return {"version": 0, "types": [], "history": []}
    return json.loads(CURRENT_FILE.read_text())


def ensure_seeded() -> dict:
    """Bootstrap the registry with the 3 builtin types. No-ops (returns the
    existing registry unchanged) once version > 0 — same one-shot contract as
    taxonomy.ensure_seeded()."""
    current = load_current()
    if current["version"] > 0:
        return current
    seeded = {"version": 0, "types": list(_BUILTIN_TYPES), "history": []}
    EXPORT_TYPES_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(json.dumps(seeded, indent=2))
    return seeded


def builtin_names() -> set[str]:
    return {t["name"] for t in _BUILTIN_TYPES}


def promote(new_type: str, categories: list[str], schema_fields: list[str], evidence: dict) -> dict:
    """Add one new emergent export type and auto-promote — no human approval,
    matching taxonomy.py's contract. `categories` is the set of taxonomy
    categories this type's cluster was drawn from (what actionability.py's
    category-fallback matches future posts against); `evidence` is the
    evolver's record of why (cluster size, hrefs, naming rationale) — kept in
    full in the versioned history, same reasoning as taxonomy.py's own
    `evidence` field."""
    EXPORT_TYPES_DIR.mkdir(parents=True, exist_ok=True)
    current = load_current()
    if not current["types"]:
        current = ensure_seeded()
    if new_type in {t["name"] for t in current["types"]}:
        return current  # already promoted (e.g. a retry) — idempotent, not a new version

    next_version = current["version"] + 1
    registry = {
        "version": next_version,
        "types": current["types"] + [{
            "name": new_type, "kind": "emergent",
            "schema_fields": schema_fields, "categories": categories,
        }],
        "history": current["history"] + [{
            "version": next_version,
            "promoted_at": datetime.now(timezone.utc).isoformat(),
            "type": new_type,
            "evidence": evidence,
        }],
    }
    (EXPORT_TYPES_DIR / f"v{next_version}.json").write_text(json.dumps(registry, indent=2))
    CURRENT_FILE.write_text(json.dumps(registry, indent=2))
    return registry
