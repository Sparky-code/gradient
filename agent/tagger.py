"""Generates deeper classification tags per item — subprocesses into this
repo's own venv (has mlx_lm), same isolation pattern as reclassify.py.
Called from reevaluator.py whenever a plan gets fully resolved (accepted or
rejected), not on every ingest pass — tags are for post-resolution enrichment,
not part of the base classify/plan loop.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from agent import cancellation, config
from agent.adapters import lora as lora_adapter

VENV_PYTHON = config.ROOT / "venv" / "bin" / "python"
WORKER = Path(__file__).parent / "_tag_worker.py"
TIMEOUT_SECONDS = 300


def generate_tags(items: list[dict]) -> list[list[str]]:
    """One tag list per item, same order as input. Returns an all-empty-lists
    result (never raises) if the venv/model isn't available or the call fails —
    tagging is an enrichment, not something that should block a plan resolving."""
    if not items or not VENV_PYTHON.exists():
        return [[] for _ in items]
    with tempfile.TemporaryDirectory() as tmp:
        in_path, out_path = Path(tmp) / "in.json", Path(tmp) / "out.json"
        in_path.write_text(json.dumps({"items": items, "adapter_path": lora_adapter.current_adapter_path()}))
        try:
            cancellation.run_cancellable(
                [str(VENV_PYTHON), str(WORKER), str(in_path), str(out_path)],
                timeout=TIMEOUT_SECONDS,
            )
            return json.loads(out_path.read_text())
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                json.JSONDecodeError, cancellation.Cancelled):
            return [[] for _ in items]
