"""Local LoRA adapter training — the real local retrain step. Where
pioneer_api.py's hosted `/felix/training-jobs` path is permanently blocked by
a billing wall, this module actually trains a specialist adapter, using
LoRA_Local (a sibling project at LORA_LOCAL_ROOT, invoked via `uv run` —
never imported, same subprocess-isolation contract as VENV_PYTHON workers
elsewhere in this package).

Adapters are versioned exactly like policy.py/taxonomy.py:
  data/state/adapters/v{n}/     — the actual trained weights, written
                                   directly there by LoRA_Local's own
                                   lora-local-train (adapter_config.json +
                                   adapters.safetensors + its own run report)
  data/state/adapters/v{n}.json — a small manifest for this version
  data/state/adapters/current.json — which version is promoted for inference

Auto-promotes with no human approval on a successful training run — same
contract as policy.py/taxonomy.py. Never raises: any failure (LoRA_Local
missing, uv missing, training error, timeout) degrades to a reported,
non-fatal result, matching pioneer_api.attempt_real_retrain()'s honesty
contract, and never blocks the local few-shot-exemplar promotion that
already runs unconditionally in pioneer.py.
"""

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agent import config
from agent.adapters import pioneer_api

ADAPTERS_DIR = config.STATE_DIR / "adapters"
CURRENT_FILE = ADAPTERS_DIR / "current.json"
TRAINING_DATA_DIR = config.STATE_DIR / "lora_training_data"

# Sibling project on this machine, not a package dependency — override with
# the LORA_LOCAL_ROOT env var if it ever lives somewhere else.
LORA_LOCAL_ROOT = Path(os.environ.get("LORA_LOCAL_ROOT", str(config.ROOT.parent.parent / "LoRA_Local")))
LORA_LOCAL_CONFIG = LORA_LOCAL_ROOT / "configs" / "moe-30b-a3b.yaml"

# Matches the model the workers this adapter augments already load
# (_tag_worker.py, _reclassify_worker.py, _taxonomy_namer_worker.py).
MODEL_ID = "mlx-community/Qwen3-30B-A3B-4bit"

TRAIN_TIMEOUT_SECONDS = 1800  # generous for a 30B-A3B model even with attention-only LoRA


def load_current() -> dict:
    if not CURRENT_FILE.exists():
        return {"version": 0, "path": None}
    return json.loads(CURRENT_FILE.read_text())


def current_adapter_path() -> str | None:
    """Absolute path to the promoted adapter, or None (base model, current
    behavior) if nothing has been promoted yet — callers pass this straight
    through to mlx_lm.load(model, adapter_path=...)."""
    current = load_current()
    if current["version"] == 0 or not current.get("path"):
        return None
    resolved = ADAPTERS_DIR / current["path"]
    return str(resolved) if resolved.exists() else None


def _availability() -> tuple[bool, str | None]:
    if not LORA_LOCAL_ROOT.exists():
        return False, f"LoRA_Local not found at {LORA_LOCAL_ROOT}"
    if not shutil.which("uv"):
        return False, "uv not found on PATH"
    return True, None


def _write_training_data(queue: list[dict]) -> None:
    TRAINING_DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Reuses pioneer_api's own SFT shaping so the hosted and local paths never
    # drift apart on what "the training data" means for this feedback queue.
    jsonl_bytes = pioneer_api._build_sft_jsonl(queue)
    (TRAINING_DATA_DIR / "train.jsonl").write_bytes(jsonl_bytes)
    # queue is tiny (RETRAIN_BATCH_SIZE=5 by default) — a real held-out split
    # would leave too few rows for mlx_lm.lora's batch_size and fail outright
    # (confirmed while verifying LoRA_Local itself). Reusing the same rows as
    # valid.jsonl is a loss-sanity check at this data scale, not a real
    # generalization measure.
    (TRAINING_DATA_DIR / "valid.jsonl").write_bytes(jsonl_bytes)


def train_and_promote(queue: list[dict]) -> dict:
    ok, reason = _availability()
    if not ok:
        return {"attempted": False, "reason": reason}

    ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    next_version = load_current()["version"] + 1
    version_dir = ADAPTERS_DIR / f"v{next_version}"

    _write_training_data(queue)

    cmd = [
        "uv", "run", "--project", str(LORA_LOCAL_ROOT), "lora-local-train",
        "--model", MODEL_ID,
        "--data", str(TRAINING_DATA_DIR),
        "--adapter-path", str(version_dir),
        "--config", str(LORA_LOCAL_CONFIG),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TRAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"attempted": True, "stage": "train", "ok": False,
                "detail": f"timed out after {TRAIN_TIMEOUT_SECONDS}s"}

    if proc.returncode != 0:
        return {"attempted": True, "stage": "train", "ok": False,
                "detail": (proc.stderr or proc.stdout)[-2000:]}

    report_path = version_dir / "lora_local_run_report.json"
    run_report = json.loads(report_path.read_text()) if report_path.exists() else {}

    manifest = {
        "version": next_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "path": f"v{next_version}",
        "queue_size": len(queue),
        "training_ok": run_report.get("training", {}).get("ok"),
        "smoke_test_ok": run_report.get("smoke_test", {}).get("ok"),
    }
    (ADAPTERS_DIR / f"v{next_version}.json").write_text(json.dumps(manifest, indent=2))
    CURRENT_FILE.write_text(json.dumps(manifest, indent=2))

    return {"attempted": True, "stage": "train", "ok": True, "promoted_version": next_version, "manifest": manifest}
