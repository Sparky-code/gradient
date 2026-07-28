"""VectorAI DB adapter — the MEMORY layer (real, self-hosted via Docker).

Actian VectorAI DB (docs.vectoraidb.actian.com) runs locally in Docker —
`docker compose up -d` (see docker-compose.yml) starts a gRPC server on
localhost:6574. This is genuine episodic memory: every high-quality post
gets embedded and stored with its plan status, so recall_similar() can
answer "have we seen something like this before, and what happened to it"
— exactly the signal a self-improving planner (and Pioneer's retraining
loop) wants. Grounding (ground_locally(), below) also runs against this same
collection now — it used to be a separate hosted KB (Senso), which was
redundant with what this collection already held; see ROADMAP.md for why
that got decoupled.

Embeddings are real local model output, not a cloud API call: embed_batch()
subprocesses into this repo's own venv/ (the same isolated env
_reclassify_worker.py uses for mlx_lm) and runs nomic-embed-text-v1.5 —
already cached locally, no network call at runtime — via
_embed_worker.py. Same isolation rationale as reclassify.py: main.py keeps
no hard torch dependency, and a slow/failed model load degrades to a stub
result instead of crashing the loop.

Model loading costs ~10s, so callers batch: embed_batch() takes N texts and
pays that cost once, not once per text. remember_posts()/recall_similar_many()
are the batch-shaped entry points loop.py actually uses; embed()/
recall_similar() are single-item conveniences built on top for callers
(tests, a future CLI) that don't need batching.

Every call degrades to a stub result on VectorAIError or a failed/timed-out
embed subprocess (container not running, model not cached, etc.) so a
missing dependency never breaks the loop — the same resilience contract as
the reclassify adapter.
"""

import json
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from actian_vectorai import (
    Distance,
    PointStruct,
    VectorAIClient,
    VectorAIError,
    VectorParams,
)

from agent import cancellation, config

HOST = "localhost:6574"
COLLECTION = "agent_memory"
TAXONOMY_CANDIDATES_COLLECTION = "taxonomy_candidates"  # "other"-bucketed posts awaiting cluster detection
TAXONOMY_ANCHORS_COLLECTION = "taxonomy_anchors"        # current category list, embedded for reuse-weighting
EXPORT_TYPE_CANDIDATES_COLLECTION = "export_type_candidates"  # actionable posts with no matching export
                                                                # type yet, awaiting cluster detection —
                                                                # same shape/purpose as TAXONOMY_CANDIDATES_
                                                                # COLLECTION, a separate collection because
                                                                # it's a different candidate pool, not decoration
EXPORT_TYPE_ANCHORS_COLLECTION = "export_type_anchors"  # already-promoted EMERGENT export types, embedded
                                                          # for reuse-weighting — mirrors TAXONOMY_ANCHORS_
                                                          # COLLECTION's role, one level up the same pattern
EMBEDDING_DIM = 256  # nomic-embed-text-v1.5 supports Matryoshka truncation to this size

CLUSTER_MIN_SCORE = 0.62   # neighbor floor for "this is the same emerging topic," calibrated a
                           # touch above MIN_RECALL_SCORE since a false cluster promotes a whole
                           # taxonomy category, not just one noisy recall hit
ANCHOR_REUSE_SCORE = 0.62  # if a candidate cluster scores this close to an EXISTING category,
                           # that's reuse, not a new one — same floor as CLUSTER_MIN_SCORE since
                           # both answer "is this genuinely the same topic," just against
                           # different collections

VENV_PYTHON = config.ROOT / "venv" / "bin" / "python"
WORKER = Path(__file__).parent.parent / "_embed_worker.py"
EMBED_TIMEOUT_SECONDS = 120

MIN_RECALL_SCORE = 0.55  # cosine floor below which a hit is noise, not a real recall — calibrated
# against nomic-embed-text-v1.5, which has a ~0.4-0.5 baseline similarity between any two unrelated
# short phrases; genuine category matches scored 0.7+ vs unrelated topics maxing out around 0.5 in
# manual testing (see conversation for the calibration run).


def embed_batch(texts: list[str], kind: Literal["document", "query"]) -> list[list[float]] | None:
    """Embed all of `texts` in one subprocess call (one model load, not N).
    Returns None if the venv/model isn't available or the call fails/times
    out — callers must treat that as "no embeddings this pass", not a crash."""
    if not texts:
        return []
    if not VENV_PYTHON.exists():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        in_path, out_path = Path(tmp) / "in.json", Path(tmp) / "out.json"
        in_path.write_text(json.dumps({"texts": texts, "kind": kind, "dim": EMBEDDING_DIM}))
        try:
            cancellation.run_cancellable(
                [str(VENV_PYTHON), str(WORKER), str(in_path), str(out_path)],
                timeout=EMBED_TIMEOUT_SECONDS,
            )
            return json.loads(out_path.read_text())
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                json.JSONDecodeError, cancellation.Cancelled):
            return None


def embed(text: str, kind: Literal["document", "query"] = "document") -> list[float] | None:
    """Single-text convenience wrapper around embed_batch()."""
    vectors = embed_batch([text], kind)
    return vectors[0] if vectors else None


def _point_id(href: str) -> str:
    """Stable id per href so remember_post() on the same post is an upsert,
    not a duplicate, and update_status() can address it directly."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, href))


def _post_text(post: dict) -> str:
    parts = [
        post.get("category"),
        post.get("subcategory"),
        post.get("action"),
        " ".join(post.get("key_facts") or []),
    ]
    return " ".join(p for p in parts if p)


def _client() -> VectorAIClient:
    client = VectorAIClient(HOST)
    client.connect()
    return client


def ensure_collection() -> None:
    with _client() as client:
        try:
            client.collections.create(
                COLLECTION,
                vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.Cosine),
            )
        except VectorAIError:
            pass  # already exists


def remember_posts(posts: list[dict], status: str = "pending") -> dict:
    """Embed and upsert a batch of classified posts as memory points in one
    round trip (one embed subprocess call + one upsert call).
    Returns {"remembered": int, "reason": str|None}."""
    postable = [p for p in posts if p.get("href")]
    if not postable:
        return {"remembered": 0, "reason": None}

    vectors = embed_batch([_post_text(p) for p in postable], kind="document")
    if vectors is None:
        return {"remembered": 0, "reason": "embedding unavailable (venv/model missing or timed out)"}

    try:
        ensure_collection()
        with _client() as client:
            client.points.upsert(COLLECTION, [
                PointStruct(
                    id=_point_id(post["href"]),
                    vector=vector,
                    payload={
                        "href": post["href"],
                        "category": post.get("category"),
                        "subcategory": post.get("subcategory"),
                        "actionable": post.get("actionable"),
                        "action": post.get("action"),
                        "status": status,
                    },
                )
                for post, vector in zip(postable, vectors)
            ])
        return {"remembered": len(postable), "reason": None}
    except VectorAIError as e:
        return {"remembered": 0, "reason": str(e)}


def remember_post(post: dict, status: str = "pending") -> dict:
    """Single-post convenience wrapper around remember_posts()."""
    result = remember_posts([post], status)
    return {"remembered": result["remembered"] > 0, "reason": result["reason"]}


def update_status(hrefs: list[str], status: str) -> dict:
    """Mark remembered posts with their real feedback outcome (accepted/
    rejected/shared/invited) so future recall reflects what actually
    happened, not just what was proposed. Returns {"updated": bool, "reason": str|None}."""
    if not hrefs:
        return {"updated": False, "reason": "no hrefs"}
    try:
        with _client() as client:
            client.points.set_payload(
                COLLECTION, {"status": status},
                ids=[_point_id(h) for h in hrefs],
            )
        return {"updated": True, "reason": None}
    except VectorAIError as e:
        return {"updated": False, "reason": str(e)}


def _search(vector: list[float], limit: int, exclude_hrefs: set[str]) -> dict:
    try:
        with _client() as client:
            hits = client.points.search(
                COLLECTION, vector=vector, limit=limit + len(exclude_hrefs),
                score_threshold=MIN_RECALL_SCORE,
            )
        memories = [
            {**hit.payload, "score": round(hit.score, 3)}
            for hit in hits
            if hit.payload and hit.payload.get("href") not in exclude_hrefs
        ][:limit]
        return {"recalled": bool(memories), "memories": memories, "source": "vectorai"}
    except VectorAIError as e:
        return {"recalled": False, "memories": [], "source": "stub", "reason": str(e)}


def recall_similar_many(
    query_texts: list[str], limit: int = 3,
    exclude_hrefs_by_text: dict[str, set[str]] | None = None,
) -> dict[str, dict]:
    """Batch version of recall_similar(): one embed subprocess call for all
    of `query_texts` (typically the interests of this pass's new plans),
    then one search per query. Returns {query_text: recall_result}."""
    exclude_hrefs_by_text = exclude_hrefs_by_text or {}
    if not query_texts:
        return {}

    vectors = embed_batch(query_texts, kind="query")
    if vectors is None:
        stub = {"recalled": False, "memories": [], "source": "stub",
                "reason": "embedding unavailable (venv/model missing or timed out)"}
        return {text: stub for text in query_texts}

    return {
        text: _search(vector, limit, exclude_hrefs_by_text.get(text, set()))
        for text, vector in zip(query_texts, vectors)
    }


def recall_similar(query_text: str, limit: int = 3, exclude_hrefs: set[str] | None = None) -> dict:
    """Single-query convenience wrapper around recall_similar_many()."""
    return recall_similar_many([query_text], limit, {query_text: exclude_hrefs or set()})[query_text]


def _flatten_citation(payload: dict, max_len: int = 200) -> str:
    """Collapse a memory point's payload into one citation line — subcategory
    as the lead, action as the body, since that's the real content every point
    already carries (see remember_posts()). Same shape Senso's ground() used
    to produce (a title + a flattened chunk), so this is a like-for-like swap
    at both call sites, not a format change downstream code needs to handle."""
    subcat = payload.get("subcategory") or ""
    action = " ".join((payload.get("action") or "").split())
    if len(action) > max_len:
        action = action[:max_len].rsplit(" ", 1)[0] + "…"
    return f"{subcat}: {action}".strip(": ")


def _ground_search(vector: list[float], limit: int, exclude_hrefs: set[str]) -> dict:
    try:
        with _client() as client:
            hits = client.points.search(
                COLLECTION, vector=vector, limit=limit + len(exclude_hrefs),
                score_threshold=MIN_RECALL_SCORE,
            )
        citations = [
            _flatten_citation(hit.payload)
            for hit in hits
            if hit.payload and hit.payload.get("href") not in exclude_hrefs
        ][:limit]
        return {"grounded": bool(citations), "citations": citations, "source": "vectorai"}
    except VectorAIError as e:
        return {"grounded": False, "citations": [], "source": "stub", "reason": str(e)}


def ground_locally_many(
    query_texts: list[str], limit: int = 3,
    exclude_hrefs_by_text: dict[str, set[str]] | None = None,
) -> dict[str, dict]:
    """Batch version of ground_locally(): one embed subprocess call for every
    query text, then one local search per vector — same batching discipline
    as recall_similar_many(), which this deliberately mirrors (same collection,
    same exclude-by-text shape). Grounds an interest in *other* posts the user
    actually saved, not a post's own content citing itself — replaces
    senso.ground(), which searched a hosted KB containing nothing this corpus
    didn't already push into it. Returns {query_text: grounding_result}."""
    exclude_hrefs_by_text = exclude_hrefs_by_text or {}
    if not query_texts:
        return {}

    vectors = embed_batch(query_texts, kind="query")
    if vectors is None:
        stub = {"grounded": False, "citations": [], "source": "stub",
                "reason": "embedding unavailable (venv/model missing or timed out)"}
        return {text: stub for text in query_texts}

    return {
        text: _ground_search(vector, limit, exclude_hrefs_by_text.get(text, set()))
        for text, vector in zip(query_texts, vectors)
    }


def ground_locally(query_text: str, limit: int = 3, exclude_hrefs: set[str] | None = None) -> dict:
    """Single-query convenience wrapper around ground_locally_many()."""
    return ground_locally_many([query_text], limit, {query_text: exclude_hrefs or set()})[query_text]


# ── taxonomy evolution — a second real collection, not a second copy of the first ──
#
# `agent_memory` (above) answers "have we seen this post before, what happened to
# it." These two collections answer a different question: "is a genuine new
# interest emerging that the current category list doesn't cover, or is this just
# the same thing under a different name." Two collections because they're
# semantically different data, not decoration — this is the multi-namespace
# pattern VectorAI DB is actually built for (RUNBOOK/GAPS_AND_FILL flagged the
# single-collection design as under-using that), applied to something real: the
# static-taxonomy gap the reclassify/policy work never touched.

def ensure_taxonomy_collections() -> None:
    _ensure_collections(TAXONOMY_CANDIDATES_COLLECTION, TAXONOMY_ANCHORS_COLLECTION)


def ensure_export_type_collections() -> None:
    """Same role as ensure_taxonomy_collections(), one level up: export_type_evolver.py's
    two collections (candidates awaiting cluster detection, anchors for
    already-promoted emergent types) instead of taxonomy_evolver.py's."""
    _ensure_collections(EXPORT_TYPE_CANDIDATES_COLLECTION, EXPORT_TYPE_ANCHORS_COLLECTION)


def _ensure_collections(*names: str) -> None:
    with _client() as client:
        for coll in names:
            try:
                client.collections.create(
                    coll, vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.Cosine),
                )
            except VectorAIError:
                pass  # already exists


def remember_candidates(posts: list[dict], collection: str = TAXONOMY_CANDIDATES_COLLECTION) -> dict:
    """Upsert a batch of posts with no home yet (taxonomy_evolver.py's 'other'-bucketed
    posts, or export_type_evolver.py's actionable-but-untyped posts — same shape,
    different candidate pool, hence the `collection` param) into VectorAI DB —
    one embed subprocess call for the whole batch, the same batching discipline
    as remember_posts(); calling embed() per-post in a loop would silently
    reintroduce the one-model-load-per-item cost that pattern exists to avoid.
    Returns {"remembered": int, "reason": str|None}."""
    postable = [p for p in posts if p.get("href")]
    if not postable:
        return {"remembered": 0, "reason": None}

    vectors = embed_batch([_post_text(p) for p in postable], kind="document")
    if vectors is None:
        return {"remembered": 0, "reason": "embedding unavailable (venv/model missing or timed out)"}

    try:
        _ensure_collections(collection)
        with _client() as client:
            client.points.upsert(collection, [
                PointStruct(id=_point_id(post["href"]), vector=vector, payload={
                    "href": post["href"], "subcategory": post.get("subcategory"),
                    "action": post.get("action"), "text": _post_text(post),
                    "category": post.get("category"),
                })
                for post, vector in zip(postable, vectors)
            ])
        return {"remembered": len(postable), "reason": None}
    except VectorAIError as e:
        return {"remembered": 0, "reason": str(e)}


def cluster_neighbors_many(
    posts: list[dict], min_score: float = CLUSTER_MIN_SCORE, limit: int = 10,
    collection: str = TAXONOMY_CANDIDATES_COLLECTION,
) -> dict[str, list[dict]]:
    """Batch version: one embed subprocess call for every post's text, then a
    cheap local search per vector (gRPC, no subprocess) — the same batching
    discipline as remember_candidates()/recall_similar_many(). Returns
    {href: [neighbor, ...]}, excluding each post's own href from its own results."""
    postable = [p for p in posts if p.get("href")]
    if not postable:
        return {}
    vectors = embed_batch([_post_text(p) for p in postable], kind="query")
    if vectors is None:
        return {p["href"]: [] for p in postable}
    return {
        post["href"]: _cluster_search(vector, min_score, limit, exclude_href=post["href"], collection=collection)
        for post, vector in zip(postable, vectors)
    }


def cluster_neighbors(post: dict, min_score: float = CLUSTER_MIN_SCORE, limit: int = 10) -> list[dict]:
    """Single-post convenience wrapper around cluster_neighbors_many()."""
    href = post.get("href")
    return cluster_neighbors_many([post], min_score, limit).get(href, []) if href else []


def _cluster_search(
    vector: list[float], min_score: float, limit: int, exclude_href: str | None,
    collection: str = TAXONOMY_CANDIDATES_COLLECTION,
) -> list[dict]:
    try:
        with _client() as client:
            hits = client.points.search(
                collection, vector=vector, limit=limit + 1,
                score_threshold=min_score,
            )
        return [
            {**hit.payload, "score": round(hit.score, 3)}
            for hit in hits
            if hit.payload and hit.payload.get("href") != exclude_href
        ][:limit]
    except VectorAIError:
        return []


def sync_anchor_embeddings(category_texts: dict[str, str], collection: str = TAXONOMY_ANCHORS_COLLECTION) -> None:
    """Upsert an embedding per existing category, built from REAL representative
    post content (subcategory/action text), not the bare category name. Verified
    empirically why this matters: embedding just "food and cooking" scored only
    0.452 against a genuine pasta-recipe cluster — statistically indistinguishable
    from "fitness and wellness" (also 0.452) and sitting right in this project's
    own documented noise floor for unrelated queries (~0.47-0.5, see BENCHMARKS.md
    §2). A short label and a full sentence occupy different regions of this
    embedding space; content-to-content is the comparison CLUSTER_MIN_SCORE was
    actually calibrated against (0.68-0.72 same-topic), so anchors need to be
    content too. Idempotent (stable id per category name — a repeat sync just
    updates the vector as more real content accumulates for that category).
    Silently no-ops on any failure — a stale/empty anchor collection just means
    reuse-checks find nothing and candidates get treated as novel, never a crash."""
    if not category_texts:
        return
    names = list(category_texts.keys())
    vectors = embed_batch([category_texts[name] for name in names], kind="document")
    if vectors is None:
        return
    try:
        _ensure_collections(collection)
        with _client() as client:
            client.points.upsert(collection, [
                PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"anchor:{name}")),
                    vector=vector, payload={"name": name},
                )
                for name, vector in zip(names, vectors)
            ])
    except VectorAIError:
        pass


def _anchor_search(
    vector: list[float], min_score: float, limit: int, exclude: str | None,
    collection: str = TAXONOMY_ANCHORS_COLLECTION,
) -> list[dict]:
    """Shared search against an anchor collection (TAXONOMY_ANCHORS_COLLECTION
    by default; export_type_evolver.py passes EXPORT_TYPE_ANCHORS_COLLECTION) —
    one gRPC call, no subprocess. Over-fetches by one extra hit when `exclude`
    is set, since the excluded name (if present) still consumes a slot in
    VectorAI DB's own `limit`. Silently degrades to [] on any VectorAIError,
    same contract as every other search helper in this module."""
    try:
        with _client() as client:
            hits = client.points.search(
                collection, vector=vector,
                limit=limit + 1 if exclude else limit, score_threshold=min_score,
            )
        return [
            {"name": h.payload["name"], "score": round(h.score, 3)}
            for h in hits if h.payload and h.payload["name"] != exclude
        ][:limit]
    except VectorAIError:
        return []


def nearest_anchor_many(
    texts: list[str], min_score: float = ANCHOR_REUSE_SCORE, exclude: str | None = None,
    collection: str = TAXONOMY_ANCHORS_COLLECTION,
) -> list[dict | None]:
    """Batch version: one embed subprocess call for every text, then a cheap
    local search per vector — same batching discipline as
    cluster_neighbors_many()/recall_similar_many(). `exclude` applies to every
    item, which is always correct for reevaluator.py's one caller: every item
    in a plan being reassigned shares the same just-rejected category."""
    if not texts:
        return []
    vectors = embed_batch(texts, kind="query")
    if vectors is None:
        return [None] * len(texts)
    return [(_anchor_search(vector, min_score, 1, exclude, collection) or [None])[0] for vector in vectors]


def nearest_anchor(
    text: str, min_score: float = ANCHOR_REUSE_SCORE, exclude: str | None = None,
    collection: str = TAXONOMY_ANCHORS_COLLECTION,
) -> dict | None:
    """Single-text convenience wrapper around nearest_anchor_many(). Two
    callers, two purposes: taxonomy_evolver.py uses this with no exclusion —
    the reuse-weighting check, a new category only gets minted if this returns
    None. reevaluator.py's batched caller passes `exclude` (the item's own
    just-rejected category) — reassignment asks "does a DIFFERENT existing
    category fit better," so the category it was just rejected from can't be
    the answer even if it's the closest match. export_type_evolver.py passes
    `collection=EXPORT_TYPE_ANCHORS_COLLECTION` to run the same reuse-check
    against already-promoted emergent export types instead of categories."""
    return nearest_anchor_many([text], min_score, exclude, collection)[0]


MULTI_LABEL_TOP_K = 5
MULTI_LABEL_MIN_SCORE = 0.55  # looser than ANCHOR_REUSE_SCORE (0.62): nearest_anchor_many() answers
# "is this genuinely the same topic as an existing category" (a strict reuse-vs-new-mint decision);
# top_k_anchors_many() answers "which categories does this post plausibly relate to," a multi-label
# membership question that wants several candidates, not one strict match — so it reuses
# MIN_RECALL_SCORE's floor (same value, same "plausible signal, not noise" reasoning) instead.


def top_k_anchors_many(
    texts: list[str], k: int = MULTI_LABEL_TOP_K, min_score: float = MULTI_LABEL_MIN_SCORE,
    exclude: str | None = None,
) -> list[list[dict]]:
    """Multi-label variant of nearest_anchor_many(): same collection, same
    embeddings, same batching discipline (one embed subprocess call for every
    text, then a cheap local search per vector) — just returns every anchor
    above `min_score`, not only the single nearest one. This is the "vector
    map of categories a post relates to" a post gets scored against, in
    addition to (never instead of) the single `category` planner.py already
    groups plans by. Returns [[{"name","score"}, ...], ...], one list per
    input text, sorted by score desc, possibly empty."""
    if not texts:
        return []
    vectors = embed_batch(texts, kind="query")
    if vectors is None:
        return [[] for _ in texts]
    return [_anchor_search(vector, min_score, k, exclude) for vector in vectors]


def top_k_anchors(
    text: str, k: int = MULTI_LABEL_TOP_K, min_score: float = MULTI_LABEL_MIN_SCORE,
    exclude: str | None = None,
) -> list[dict]:
    """Single-text convenience wrapper around top_k_anchors_many()."""
    return top_k_anchors_many([text], k, min_score, exclude)[0]
