"""Ingestion: turn a dropped file into a list of classified posts.

There is no live API for a personal account's saved/liked Instagram posts —
the only sanctioned path is Meta's manual "Download Your Data" export. So the
loop's input trigger is a watched folder (config.DROP_DIR): a new file dropped
there is the ingestion event, not a live poll of Instagram itself.

Gradient's own job starts *after* a raw export has already been turned into
classified posts — planning, grounding, taxonomy evolution, and the feedback
loop, not downloading/transcribing/OCR'ing raw Instagram media. So the only
shape `load_posts()` accepts is already-enriched: a list of objects each
carrying at least an "actionable" field (plus "href"/"category"/"subcategory"/
"action"/"key_facts" — see fixtures/demo_export.json for a real example).

This used to also accept a raw export (hrefs only) and shell out to a sibling
project (InstaGone) via a hard-coded absolute path to enrich it in place. That
coupling is gone on purpose: Gradient shouldn't hard-depend on one specific
external enrichment tool living at one specific path on one machine. Turning a
raw export into the enriched shape above is still a real, separate step
someone needs to run — just outside this repo, with whatever tool they choose
(InstaGone is one option) — and its output dropped into config.DROP_DIR like
any other input.
"""

import json
from pathlib import Path


def _is_enriched(posts: list[dict]) -> bool:
    return bool(posts) and "actionable" in posts[0]


def load_posts(drop_file: Path) -> list[dict]:
    data = json.loads(Path(drop_file).read_text())
    posts = data if isinstance(data, list) else data.get("posts", [])

    if not _is_enriched(posts):
        raise RuntimeError(
            f"{drop_file} has no 'actionable' field on its first post — Gradient only "
            "accepts already-enriched/classified posts (href, category, subcategory, "
            "actionable, action, key_facts). Raw Instagram exports need to be enriched "
            "by a separate tool first; Gradient doesn't shell out to one itself. See "
            "fixtures/demo_export.json for the expected shape."
        )

    return posts
