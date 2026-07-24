"""Ingestion: turn a dropped file into a list of classified posts.

There is no live API for a personal account's saved/liked Instagram posts —
the only sanctioned path is Meta's manual "Download Your Data" export. So the
loop's input trigger is a watched folder (config.DROP_DIR): a new file dropped
there is the ingestion event, not a live poll of Instagram itself.

A dropped file is one of two shapes:
  - already enriched (has an "actionable" key per post) -> InstaGone's
    analyze.py already ran on it; load directly.
  - a raw Instagram export (hrefs only) -> shell out to InstaGone's analyze.py
    in its own venv to run the full download/transcribe/OCR/classify pipeline.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from agent import config


def _is_enriched(posts: list[dict]) -> bool:
    return bool(posts) and "actionable" in posts[0]


def load_posts(drop_file: Path) -> list[dict]:
    data = json.loads(Path(drop_file).read_text())
    posts = data if isinstance(data, list) else data.get("posts", [])

    if _is_enriched(posts):
        return posts

    if not config.INSTAGONE_PYTHON.exists():
        raise RuntimeError(
            f"{drop_file} looks like a raw export (no 'actionable' field) and "
            f"InstaGone's venv wasn't found at {config.INSTAGONE_PYTHON} to enrich it."
        )

    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "enriched.json"
        subprocess.run(
            [str(config.INSTAGONE_PYTHON), str(config.INSTAGONE_ANALYZE),
             str(drop_file), "--output", str(out_path), "--resume"],
            cwd=config.INSTAGONE_DIR, check=True,
        )
        return json.loads(out_path.read_text())
