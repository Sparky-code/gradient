"""Self-evolving category taxonomy — the piece the audit flagged as missing:
suppression (policy.py/reclassify.py) is the only thing that evolved before this,
and the anchor category list itself was completely frozen. This closes that gap
using the two tools already proven real in this project, in new roles:

  VectorAI DB — a genuine multi-collection use (taxonomy_candidates,
  taxonomy_anchors), not a second copy of agent_memory: detects whether "other"
  posts recur into a real cluster, and checks that cluster against existing
  categories before minting anything new (the reuse-weighting explicitly asked
  for — a category only gets promoted if it's NOT already covered).

  Senso — a new role for it too: grounding a *candidate taxonomy expansion*
  (does real external content support this being a coherent topic?) rather than
  citing sources for an already-decided plan. Senso's own content-generation
  endpoint (POST /org/content-generation/sample) was evaluated and rejected for
  the actual naming step — it requires pre-configured content-types/GEO
  questions built for SEO/marketing copy, a 30-90s async job, and no free-form
  prompt path; forcing a category-naming task through that shape would repeat
  the exact mistake reclassify.py already made once (an API used outside what
  it's built for). Naming stays with the local model that's already reliable
  for this kind of judgment call.

Auto-promotes with no human approval, same contract as policy.py/pioneer.py.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from agent import cancellation, config, taxonomy
from agent.adapters import guild, senso, vectorai

CLUSTER_MIN_SIZE = 3  # itself + at least 2 real neighbors — mirrors Pioneer's
                       # RETRAIN_BATCH_SIZE=5 in spirit: don't act on a single post
NAMER_WORKER = Path(__file__).parent / "_taxonomy_namer_worker.py"
VENV_PYTHON = config.ROOT / "venv" / "bin" / "python"
NAMER_TIMEOUT_SECONDS = 300


def _propose_name(cluster_posts: list[dict], citations: list[str], existing_categories: list[str]) -> dict:
    if not VENV_PYTHON.exists():
        return {"category": None, "description": None}
    with tempfile.TemporaryDirectory() as tmp:
        in_path, out_path = Path(tmp) / "in.json", Path(tmp) / "out.json"
        in_path.write_text(json.dumps({
            "cluster_posts": cluster_posts, "citations": citations,
            "existing_categories": existing_categories,
        }))
        try:
            cancellation.run_cancellable(
                [str(VENV_PYTHON), str(NAMER_WORKER), str(in_path), str(out_path)],
                timeout=NAMER_TIMEOUT_SECONDS,
            )
            return json.loads(out_path.read_text())
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                json.JSONDecodeError, cancellation.Cancelled):
            return {"category": None, "description": None}


def _category_representative_texts(posts: list[dict], categories: list[str]) -> dict[str, str]:
    """Real subcategory/action content per existing category, for
    sync_anchor_embeddings() — a bare category name isn't a fair embedding
    comparison against a candidate cluster's full sentences (see that
    function's docstring for the measured scores proving this)."""
    texts: dict[str, str] = {}
    for category in categories:
        matches = [p for p in posts if p.get("category") == category][:5]
        if matches:
            texts[category] = " ".join(
                f"{p.get('subcategory') or ''} {p.get('action') or ''}" for p in matches
            ).strip()
    return texts


def evolve(posts: list[dict]) -> dict:
    """Run one taxonomy-evolution pass. `posts` is this pass's FULL post list
    (not just the "other" ones) — needed so existing categories can be embedded
    from real representative content, not just their bare names. Returns a
    summary dict for logging — never raises; every real-tool call already
    degrades to a safe no-op on its own (VectorAI DB/Senso/local model), so a
    full pass here only ever adds zero or one new category, never crashes the
    loop it's called from."""
    current = taxonomy.load_current()
    other_posts = [p for p in posts if p.get("category") == "other"]
    if not other_posts:
        return {"candidates_seen": 0, "cluster_found": False, "promoted": None}

    vectorai.sync_anchor_embeddings(_category_representative_texts(posts, current["categories"]))
    vectorai.remember_candidates(other_posts)

    # One batched embed call for every post's neighbor search, not one per post.
    neighbors_by_href = vectorai.cluster_neighbors_many(other_posts)

    # Only need to find ONE real cluster per pass — evolve() runs every pass, so
    # a second emerging topic just gets picked up next time.
    for post in other_posts:
        neighbors = neighbors_by_href.get(post.get("href"), [])
        if len(neighbors) + 1 < CLUSTER_MIN_SIZE:  # +1 for the post itself
            continue

        cluster_posts = [post] + [
            {"href": n.get("href"), "subcategory": n.get("subcategory"), "action": n.get("action")}
            for n in neighbors
        ]
        representative_text = " ".join(
            f"{p.get('subcategory') or ''} {p.get('action') or ''}" for p in cluster_posts
        ).strip()

        reuse_match = vectorai.nearest_anchor(representative_text)
        if reuse_match:
            guild.log_session({
                "event": "taxonomy_reuse_skip", "cluster_size": len(cluster_posts),
                "representative_href": post.get("href"), "matched_existing": reuse_match["name"],
                "score": reuse_match["score"],
            })
            return {
                "candidates_seen": len(other_posts), "cluster_found": True,
                "promoted": None, "reuse_matched": reuse_match["name"],
            }

        grounding = senso.ground(representative_text)
        if not grounding["grounded"]:
            guild.log_session({
                "event": "taxonomy_cluster_ungrounded", "cluster_size": len(cluster_posts),
                "representative_href": post.get("href"),
            })
            return {"candidates_seen": len(other_posts), "cluster_found": True, "promoted": None}

        proposal = _propose_name(cluster_posts, grounding["citations"], current["categories"])
        if not proposal.get("category"):
            guild.log_session({
                "event": "taxonomy_naming_failed", "cluster_size": len(cluster_posts),
                "representative_href": post.get("href"),
            })
            return {"candidates_seen": len(other_posts), "cluster_found": True, "promoted": None}

        promoted = taxonomy.promote(proposal["category"], evidence={
            "cluster_size": len(cluster_posts),
            "cluster_hrefs": [p.get("href") for p in cluster_posts if p.get("href")],
            "description": proposal.get("description"),
            "senso_citations": grounding["citations"],
            "reuse_check_cleared": True,
        })
        guild.log_session({
            "event": "taxonomy_promoted", "category": proposal["category"],
            "version": promoted["version"], "cluster_size": len(cluster_posts),
        })
        return {
            "candidates_seen": len(other_posts), "cluster_found": True,
            "promoted": proposal["category"], "taxonomy_version": promoted["version"],
        }

    return {"candidates_seen": len(other_posts), "cluster_found": False, "promoted": None}
