"""Source-quality composite for learning-plan grounding (ROADMAP.md §3) — the
piece that decides whether an external search result is actually worth
citing, not just whatever a search backend returned first.

No true automatable "industry standard" exists for this (researched, not
guessed): E-E-A-T is Google-internal with no API; the CRAAP test is a
human-judgment checklist; NewsGuard is sales-gated; citation metrics only
cover academic papers, not general web pages. The composite here combines
three signals that ARE cheaply computable:

  1. Structural metadata heuristics (_structural_score) — HTTPS, byline/
     about-page markers when page text is available, a published date —
     free, deterministic, no model call.
  2. An LLM-as-judge rubric pass (_source_quality_worker.py, same
     subprocess-isolation pattern as _tag_worker.py) — the closest
     automatable stand-in for CRAAP/E-E-A-T's human-judgment dimensions
     (does this read as credible, first-hand/expert content, or vague/
     promotional). Deliberately does NOT reuse the classification LoRA
     adapter (agent/adapters/lora.py) — that's trained on Instagram-post
     accept/reject signal, an unrelated judgment task; applying it here
     would bias the model toward post-categorization patterns, not source
     credibility.
  3. Cross-source corroboration (_corroboration_flags) — do two or more
     independent-domain results in the same search agree on similar
     content — nearly free since a multi-result search call already
     returns the candidates to compare.

Operates entirely on agent/adapters/external_search.py's `search()` result
shape (url/title/excerpt/raw_content/published_date) — vendor-agnostic by
construction, so this doesn't wait on which backend gets wired in there.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from agent import cancellation, config

VENV_PYTHON = config.ROOT / "venv" / "bin" / "python"
WORKER = Path(__file__).parent / "_source_quality_worker.py"
TIMEOUT_SECONDS = 300

_BYLINE_MARKERS = ("by ", "author:", "written by", "posted by")
_INFO_MARKERS = ("about us", "about the author", "contact us", "our mission")

# A shared claim between two DIFFERENT domains is real corroboration; a
# handful of common English words matching isn't — this threshold is on
# Jaccard overlap of non-trivial keywords, tuned to require genuine topical
# overlap rather than incidental word reuse.
_COOCCURRENCE_MIN_OVERLAP = 0.25
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "in", "on",
    "at", "to", "for", "of", "with", "this", "that", "it", "as", "by", "from",
    "be", "how", "what", "your", "you", "will", "can", "not", "has", "have",
}


def _structural_score(result: dict) -> float:
    """Free, deterministic, no model call — the mechanical half of the
    composite. Byline/about-page markers only fire when page text is
    actually available (raw_content, or excerpt as a weaker fallback) —
    a snippet-only backend just scores lower on this half, not an error."""
    score = 0.0
    if (result.get("url") or "").startswith("https://"):
        score += 0.4
    text = (result.get("raw_content") or result.get("excerpt") or "").lower()
    if any(m in text for m in _BYLINE_MARKERS):
        score += 0.3
    if any(m in text for m in _INFO_MARKERS):
        score += 0.15
    if result.get("published_date"):
        score += 0.15
    return round(min(1.0, score), 2)


def domain_of(url: str) -> str:
    return urlparse(url or "").netloc.removeprefix("www.").lower()


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _corroboration_flags(results: list[dict]) -> list[bool]:
    """True for a result if at least one OTHER, DIFFERENT-domain result in
    the same set shares enough topical keyword overlap to count as
    independent agreement — same-domain matches don't count (that's one
    source repeating itself, not corroboration)."""
    keyword_sets = [_keywords(r.get("excerpt") or "") for r in results]
    domains = [domain_of(r.get("url") or "") for r in results]

    flags = []
    for i in range(len(results)):
        corroborated = False
        for j in range(len(results)):
            if i == j or not domains[i] or domains[i] == domains[j]:
                continue
            kw_i, kw_j = keyword_sets[i], keyword_sets[j]
            if not kw_i or not kw_j:
                continue
            overlap = len(kw_i & kw_j) / len(kw_i | kw_j)
            if overlap >= _COOCCURRENCE_MIN_OVERLAP:
                corroborated = True
                break
        flags.append(corroborated)
    return flags


def _overall(structural: float, llm_score: float | None, corroborated: bool) -> float:
    base = (structural + llm_score) / 2 if llm_score is not None else structural
    if corroborated:
        base = min(1.0, base + 0.15)
    return round(base, 2)


def _judge_via_worker(results: list[dict]) -> list[dict]:
    """One {"score": float|None, "reason": str|None} per result, same order.
    Degrades to all-None scores (never raises) if the venv/model isn't
    available or the call fails — matching every other local-model call in
    this codebase (tagger.py, reclassify.py): a failed judgment drops out of
    the overall blend rather than blocking or crashing scoring."""
    if not results or not VENV_PYTHON.exists():
        return [{"score": None, "reason": None} for _ in results]
    with tempfile.TemporaryDirectory() as tmp:
        in_path, out_path = Path(tmp) / "in.json", Path(tmp) / "out.json"
        in_path.write_text(json.dumps({"results": results}))
        try:
            cancellation.run_cancellable(
                [str(VENV_PYTHON), str(WORKER), str(in_path), str(out_path)],
                timeout=TIMEOUT_SECONDS,
            )
            return json.loads(out_path.read_text())
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                json.JSONDecodeError, cancellation.Cancelled):
            return [{"score": None, "reason": None} for _ in results]


def score_sources(results: list[dict]) -> list[dict]:
    """Enriches each of agent/adapters/external_search.py's `search()`
    results with a "quality" dict. Never raises — every sub-signal already
    degrades gracefully on its own (see each function's docstring)."""
    if not results:
        return []

    structural = [_structural_score(r) for r in results]
    corroborated = _corroboration_flags(results)
    judged = _judge_via_worker(results)

    enriched = []
    for result, struct_score, is_corroborated, judgment in zip(results, structural, corroborated, judged):
        enriched.append({
            **result,
            "quality": {
                "structural_score": struct_score,
                "llm_judge_score": judgment["score"],
                "llm_judge_reason": judgment["reason"],
                "corroborated": is_corroborated,
                "overall": _overall(struct_score, judgment["score"], is_corroborated),
            },
        })
    return enriched
