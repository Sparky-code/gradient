"""Self-evolving export-type registry — the same auto-promotion pattern
agent/taxonomy_evolver.py already applies to categories, applied one level up
to the *kind* of actionable content itself (music/location/recipe are seed
examples, not a closed set — see agent/export_types.py).

Mirrors taxonomy_evolver.py's pipeline closely, reusing the same VectorAI DB
clustering/reuse-check/grounding/naming primitives against a parallel pair of
collections (export_type_candidates/export_type_anchors) instead of
duplicating that logic:

  Clustering — detects whether actionable posts that don't match any current
  export type (agent/actionability.py already tried and failed to place them)
  recur into a real cluster.

  Reuse-check — an emergent export type only gets minted if the cluster
  doesn't already score close to an existing EMERGENT type's anchor (builtin
  types are keyword-matched in actionability.py and never reach here).

  Grounding — same local semantic-search-against-other-saved-posts check
  taxonomy_evolver.py uses, via vectorai.ground_locally().

  Naming — a local model (_export_type_namer_worker.py) proposes a short
  noun-phrase name for the kind of actionable output this cluster represents.

Auto-promotes with no human approval, same contract as taxonomy_evolver.py/
policy.py/pioneer.py. This LLM call only fires on a genuine recurring
cluster, not per-post — same latency profile as taxonomy_evolver.py.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from agent import cancellation, config, export_types, planner, session_log
from agent.adapters import vectorai

CLUSTER_MIN_SIZE = 3  # same bar as taxonomy_evolver.py's CLUSTER_MIN_SIZE — itself + at
                       # least 2 real neighbors, don't act on a single post
NAMER_WORKER = Path(__file__).parent / "_export_type_namer_worker.py"
VENV_PYTHON = config.ROOT / "venv" / "bin" / "python"
NAMER_TIMEOUT_SECONDS = 300


def _propose_type(cluster_posts: list[dict], citations: list[str], existing_types: list[str]) -> dict:
    if not VENV_PYTHON.exists():
        return {"type": None, "description": None}
    with tempfile.TemporaryDirectory() as tmp:
        in_path, out_path = Path(tmp) / "in.json", Path(tmp) / "out.json"
        in_path.write_text(json.dumps({
            "cluster_posts": cluster_posts, "citations": citations,
            "existing_types": existing_types,
        }))
        try:
            cancellation.run_cancellable(
                [str(VENV_PYTHON), str(NAMER_WORKER), str(in_path), str(out_path)],
                timeout=NAMER_TIMEOUT_SECONDS,
            )
            return json.loads(out_path.read_text())
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                json.JSONDecodeError, cancellation.Cancelled):
            return {"type": None, "description": None}


def evolve(posts: list[dict]) -> dict:
    """Run one export-type-evolution pass. `posts` must already have
    entity_type/entity_fields set by agent/actionability.py (see
    orchestrator.py's actionability-router, which runs first, and
    category-mapper before that, which is why category_scores is already
    available too). Returns a summary dict for logging — never raises; every
    real-tool call already degrades to a safe no-op on its own (VectorAI
    DB/local model), so a full pass here only ever promotes zero or one new
    export type, never crashes the loop it's called from."""
    untyped = [
        p for p in posts
        if p.get("entity_type") is None and p.get("actionable") not in planner.LOW_QUALITY_ACTIONABLE
    ]
    if not untyped:
        return {"candidates_seen": 0, "cluster_found": False, "promoted": None}

    current = export_types.ensure_seeded()
    emergent_types = [t for t in current["types"] if t["kind"] == "emergent"]
    anchor_texts = {t["name"]: " ".join(t.get("categories") or []) or t["name"] for t in emergent_types}
    vectorai.sync_anchor_embeddings(anchor_texts, collection=vectorai.EXPORT_TYPE_ANCHORS_COLLECTION)
    vectorai.remember_candidates(untyped, collection=vectorai.EXPORT_TYPE_CANDIDATES_COLLECTION)

    # One batched embed call for every post's neighbor search, not one per post.
    neighbors_by_href = vectorai.cluster_neighbors_many(
        untyped, collection=vectorai.EXPORT_TYPE_CANDIDATES_COLLECTION,
    )

    # Only need to find ONE real cluster per pass — evolve() runs every pass, so
    # a second emerging export type just gets picked up next time.
    for post in untyped:
        neighbors = neighbors_by_href.get(post.get("href"), [])
        if len(neighbors) + 1 < CLUSTER_MIN_SIZE:  # +1 for the post itself
            continue

        cluster_posts = [post] + [
            {"href": n.get("href"), "subcategory": n.get("subcategory"),
             "action": n.get("action"), "category": n.get("category")}
            for n in neighbors
        ]
        representative_text = " ".join(
            f"{p.get('subcategory') or ''} {p.get('action') or ''}" for p in cluster_posts
        ).strip()

        reuse_match = vectorai.nearest_anchor(
            representative_text, collection=vectorai.EXPORT_TYPE_ANCHORS_COLLECTION,
        )
        if reuse_match:
            session_log.log_session({
                "event": "export_type_reuse_skip", "cluster_size": len(cluster_posts),
                "representative_href": post.get("href"), "matched_existing": reuse_match["name"],
                "score": reuse_match["score"],
            })
            return {
                "candidates_seen": len(untyped), "cluster_found": True,
                "promoted": None, "reuse_matched": reuse_match["name"],
            }

        # Exclude the cluster's own member posts — grounding has to find
        # OTHER saved content supporting this being a real, recurring kind of
        # actionable content, not just echo the exact posts being evaluated.
        grounding = vectorai.ground_locally(
            representative_text,
            exclude_hrefs={p.get("href") for p in cluster_posts if p.get("href")},
        )
        if not grounding["grounded"]:
            session_log.log_session({
                "event": "export_type_cluster_ungrounded", "cluster_size": len(cluster_posts),
                "representative_href": post.get("href"),
            })
            return {"candidates_seen": len(untyped), "cluster_found": True, "promoted": None}

        proposal = _propose_type(cluster_posts, grounding["citations"], [t["name"] for t in current["types"]])
        if not proposal.get("type"):
            session_log.log_session({
                "event": "export_type_naming_failed", "cluster_size": len(cluster_posts),
                "representative_href": post.get("href"),
            })
            return {"candidates_seen": len(untyped), "cluster_found": True, "promoted": None}

        cluster_categories = sorted({p.get("category") for p in cluster_posts if p.get("category")})
        promoted = export_types.promote(
            proposal["type"], categories=cluster_categories,
            schema_fields=["key_facts", "action", "tags"],
            evidence={
                "cluster_size": len(cluster_posts),
                "cluster_hrefs": [p.get("href") for p in cluster_posts if p.get("href")],
                "description": proposal.get("description"),
                "grounding_citations": grounding["citations"],
                "reuse_check_cleared": True,
            },
        )
        session_log.log_session({
            "event": "export_type_promoted", "type": proposal["type"],
            "version": promoted["version"], "cluster_size": len(cluster_posts),
        })
        return {
            "candidates_seen": len(untyped), "cluster_found": True,
            "promoted": proposal["type"], "export_types_version": promoted["version"],
        }

    return {"candidates_seen": len(untyped), "cluster_found": False, "promoted": None}
