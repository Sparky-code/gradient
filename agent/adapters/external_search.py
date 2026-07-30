"""External web search — grounds a learning plan in real material beyond the
user's own saved posts (ROADMAP.md §3, "learning plans"). This is the one
deliberately-stubbed adapter in that feature: which backend to call (Exa,
Tavily, or a self-hosted SearXNG) is still being researched — see the
project owner's own comparison in that section — and is a real vendor
decision, not an implementation detail to guess at.

Every other piece of the learning-plan pipeline (source-quality scoring:
structural heuristics + LLM-judge + cross-source corroboration; usage-based
trust curation) is built against this module's `search()` contract, not
against any one vendor's response shape, so wiring in a real backend later
is a one-file change here — same real-call-vs-local-fallback split as
pioneer_api.py/pioneer.py, just not yet past the "which real call" decision.

Contract every backend (real or stub) must satisfy:
  search(query, max_results) -> {
    "grounded": bool,
    "source": "exa" | "tavily" | "searxng" | "stub",
    "results": [{
        "url": str, "title": str, "excerpt": str,
        "raw_content": str | None, "published_date": str | None,
    }, ...],
    "reason": str | None,   # why grounded=False, mirroring every other adapter's stub contract
  }
`raw_content` and `published_date` are the two fields that vary by vendor
(Exa/Tavily can return full page text and a date; SearXNG returns snippets
only, no date) — deliberately optional so downstream code (source-quality
scoring, see agent/source_quality.py) works off `excerpt`/`url` alone and
only uses the others as bonus signals when a backend happens to provide them.
"""


def search(query: str, max_results: int = 5) -> dict:
    """Always returns the stub shape today — no backend chosen/configured
    yet. Never raises: once a real backend is wired in here, this keeps the
    same degrade-with-reason contract on that backend's own failures
    (network error, rate limit, no API key), so nothing downstream needs to
    change when the stub is replaced."""
    return {
        "grounded": False,
        "source": "stub",
        "results": [],
        "reason": "no external search backend configured yet — see ROADMAP.md §3",
    }
