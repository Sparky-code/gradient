"""Senso adapter — the KNOWLEDGE layer.

Two real, verified calls against the live API (base URL + auth header +
both endpoint shapes confirmed via docs.senso.ai/docs/concepts and
docs.senso.ai/docs/knowledge-base, plus a live test ingest that returned a
real content_id):

  ingest_post()  -> POST /org/kb/raw    push a classified post into the KB
  ground()       -> POST /org/search/context   pull raw chunks back out

Ingestion is async ("processing_status": "processing" on creation per the
live test call) — a post ingested this pass may not be searchable until a
later pass. ground() always degrades to a stub on any failure so a Senso
outage or empty KB never breaks the publish step.
"""

import json
import urllib.error
import urllib.request

from agent import config

BASE_URL = "https://apiv2.senso.ai/api/v1"
SEARCH_PATH = "/org/search/context"
INGEST_PATH = "/org/kb/raw"

# Cloudflare blocks urllib's default "Python-urllib/x.y" UA (403, error 1010)
# in front of Senso's API — a normal browser UA clears it. Confirmed live.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _api_key() -> str | None:
    return config.load_api_key("Senso")


def _post(path: str, body: dict, key: str) -> dict:
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "X-API-Key": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _UA,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ingest_post(post: dict) -> dict:
    """Push one classified post into Senso's knowledge base as raw text so
    future ground() calls have real content to retrieve. Returns
    {"ingested": bool, "content_id": str|None, "reason": str|None}."""
    key = _api_key()
    if not key:
        return {"ingested": False, "content_id": None, "reason": "no API key configured"}

    title = f"{post.get('subcategory') or post.get('category') or 'Instagram post'}"
    text_parts = [
        f"Category: {post.get('category')} / {post.get('subcategory')}",
        f"Action: {post.get('action')}" if post.get("action") else None,
        f"Key facts: {'; '.join(post.get('key_facts') or [])}" if post.get("key_facts") else None,
        f"Source: {post.get('href')}",
    ]
    text = "\n".join(p for p in text_parts if p)

    try:
        payload = _post(INGEST_PATH, {
            "title": title,
            "text": text,
            "summary": post.get("action") or title,
        }, key)
        return {"ingested": True, "content_id": payload.get("id"), "reason": None}
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        return {"ingested": False, "content_id": None, "reason": str(e)}


def _flatten_chunk(result: dict, max_len: int = 200) -> str:
    """chunk_text comes back multi-line (we ingested it that way) — collapse
    to one line so it doesn't break the markdown list it's rendered into."""
    text = " ".join((result.get("chunk_text") or "").split())
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    title = result.get("title", "")
    return f"{title}: {text}".strip(": ")


def ground(query: str, max_results: int = 3) -> dict:
    """Query Senso for raw context chunks relevant to `query`.

    Returns {"grounded": bool, "citations": [str, ...], "source": "senso"|"stub"}.
    """
    key = _api_key()
    if not key:
        return {"grounded": False, "citations": [], "source": "stub", "reason": "no API key configured"}

    try:
        payload = _post(SEARCH_PATH, {"query": query, "max_results": max_results}, key)
        results = payload.get("results") or []
        citations = [_flatten_chunk(r) for r in results[:max_results]]
        return {"grounded": bool(citations), "citations": citations, "source": "senso"}
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        return {"grounded": False, "citations": [], "source": "stub", "reason": str(e)}
