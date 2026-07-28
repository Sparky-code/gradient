"""Self-evolving category taxonomy — the piece the audit flagged as missing:
suppression (policy.py/reclassify.py) is the only thing that evolved before this,
and the anchor category list itself was completely frozen. This closes that gap
using VectorAI DB, in two roles:

  Clustering — a genuine multi-collection use (taxonomy_candidates,
  taxonomy_anchors), not a second copy of agent_memory: detects whether "other"
  posts recur into a real cluster, and checks that cluster against existing
  categories before minting anything new (the reuse-weighting explicitly asked
  for — a category only gets promoted if it's NOT already covered).

  Grounding — checking a *candidate taxonomy expansion* against other posts
  the user actually saved (does content beyond this exact cluster support it
  being a coherent, recurring topic?) rather than citing sources for an
  already-decided plan. This used to be Senso's job (a hosted KB search); it's
  local now (`vectorai.ground_locally()`) — see ROADMAP.md for why that
  decoupling happened. Naming stays with the local model, same as before —
  that was never Senso's or VectorAI DB's job.

Auto-promotes with no human approval, same contract as policy.py/pioneer.py.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from agent import cancellation, config, session_log, taxonomy
from agent.adapters import lora as lora_adapter
from agent.adapters import vectorai

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
            "adapter_path": lora_adapter.current_adapter_path(),
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
    degrades to a safe no-op on its own (VectorAI DB/local model), so a
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
            session_log.log_session({
                "event": "taxonomy_reuse_skip", "cluster_size": len(cluster_posts),
                "representative_href": post.get("href"), "matched_existing": reuse_match["name"],
                "score": reuse_match["score"],
            })
            return {
                "candidates_seen": len(other_posts), "cluster_found": True,
                "promoted": None, "reuse_matched": reuse_match["name"],
            }

        # Exclude the cluster's own member posts — grounding has to find
        # OTHER saved content supporting this being a real topic, not just
        # echo the exact posts already being evaluated back at themselves.
        grounding = vectorai.ground_locally(
            representative_text,
            exclude_hrefs={p.get("href") for p in cluster_posts if p.get("href")},
        )
        if not grounding["grounded"]:
            session_log.log_session({
                "event": "taxonomy_cluster_ungrounded", "cluster_size": len(cluster_posts),
                "representative_href": post.get("href"),
            })
            return {"candidates_seen": len(other_posts), "cluster_found": True, "promoted": None}

        proposal = _propose_name(cluster_posts, grounding["citations"], current["categories"])
        if not proposal.get("category"):
            session_log.log_session({
                "event": "taxonomy_naming_failed", "cluster_size": len(cluster_posts),
                "representative_href": post.get("href"),
            })
            return {"candidates_seen": len(other_posts), "cluster_found": True, "promoted": None}

        promoted = taxonomy.promote(proposal["category"], evidence={
            "cluster_size": len(cluster_posts),
            "cluster_hrefs": [p.get("href") for p in cluster_posts if p.get("href")],
            "description": proposal.get("description"),
            "grounding_citations": grounding["citations"],
            "reuse_check_cleared": True,
        })
        session_log.log_session({
            "event": "taxonomy_promoted", "category": proposal["category"],
            "version": promoted["version"], "cluster_size": len(cluster_posts),
        })
        return {
            "candidates_seen": len(other_posts), "cluster_found": True,
            "promoted": proposal["category"], "taxonomy_version": promoted["version"],
        }

    return {"candidates_seen": len(other_posts), "cluster_found": False, "promoted": None}
