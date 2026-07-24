"""Applies the current Pioneer-promoted policy (few-shot exemplars from real
accept/reject feedback) to new posts via a real local model call — this is
what proves the retrain loop changes behavior, not just logs a number.

Subprocesses into this repo's own venv (has mlx_lm) rather than importing
directly, so `main.py` has no hard ML dependency and a slow/failed model
load degrades to "posts unchanged" instead of crashing the loop.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from agent import cancellation, config

VENV_PYTHON = config.ROOT / "venv" / "bin" / "python"
WORKER = Path(__file__).parent / "_reclassify_worker.py"
TIMEOUT_SECONDS = 300


def apply_policy(posts: list[dict], policy: dict) -> tuple[list[dict], list[dict]]:
    """Returns (posts_with_policy_fields, changes) where `changes` lists posts
    the policy suppressed with high confidence — i.e. this post's
    subcategory/action closely resembles something the user explicitly
    rejected before. The model never reassigns `category` here (an earlier
    version let it, and an 8B model reliably hallucinated nonsensical
    reassignments under that freedom); it only judges surface-worthiness."""
    if not policy.get("exemplars") or not VENV_PYTHON.exists():
        return posts, []

    with tempfile.TemporaryDirectory() as tmp:
        in_path, out_path = Path(tmp) / "in.json", Path(tmp) / "out.json"
        in_path.write_text(json.dumps({"posts": posts, "policy": policy}))
        try:
            cancellation.run_cancellable(
                [str(VENV_PYTHON), str(WORKER), str(in_path), str(out_path)],
                timeout=TIMEOUT_SECONDS,
            )
            reclassified = json.loads(out_path.read_text())
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                json.JSONDecodeError, cancellation.Cancelled):
            return posts, []

    changes = [
        p for p in reclassified
        if p.get("policy_confidence") == "high" and p.get("should_surface") is False
    ]
    return reclassified, changes
