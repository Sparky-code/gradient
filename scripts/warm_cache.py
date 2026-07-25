#!/usr/bin/env python3
"""Pre-warm both local model caches before a demo run.

Both local models auto-download from Hugging Face on first use, but
agent/_embed_worker.py forces HF_HUB_OFFLINE=1 once running — on a machine
where nomic-embed-text has never been fetched, that means it fails loudly
instead of quietly downloading mid-demo. Run this once, ahead of time, on any
new machine (or after a fresh `venv/`/HF cache wipe):

    ./venv/bin/python scripts/warm_cache.py

Packages what used to be two copy-pasted shell one-liners in
GAPS_AND_FILL.md Part 1 §2 — same two calls, same model IDs, just a real
script instead of prose to retype.
"""

import os
import sys
import time

# Must be set before `transformers` is imported anywhere in this process —
# the embed worker's own HF_HUB_OFFLINE=1 (agent/_embed_worker.py) only makes
# sense once the model is already cached on disk; fetching it here requires
# the opposite.
os.environ["HF_HUB_OFFLINE"] = "0"

RECLASSIFY_MODEL_ID = "mlx-community/Qwen3-30B-A3B-4bit"  # also used by _tag_worker.py, _taxonomy_namer_worker.py
EMBED_MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"


def _warm_reclassify_model() -> bool:
    print(f"[1/2] fetching {RECLASSIFY_MODEL_ID} (~16GB, first time only)...")
    t0 = time.time()
    try:
        from mlx_lm import load
        load(RECLASSIFY_MODEL_ID)
    except Exception as e:  # noqa: BLE001 - report and let the summary decide the exit code
        print(f"      FAILED: {e}")
        return False
    print(f"      done in {time.time() - t0:.0f}s")
    return True


def _warm_embed_model() -> bool:
    print(f"[2/2] fetching {EMBED_MODEL_ID} (offline guard disabled for this fetch)...")
    t0 = time.time()
    try:
        from transformers import AutoModel, AutoTokenizer
        AutoModel.from_pretrained(EMBED_MODEL_ID, trust_remote_code=True)
        AutoTokenizer.from_pretrained(EMBED_MODEL_ID)
    except Exception as e:  # noqa: BLE001 - report and let the summary decide the exit code
        print(f"      FAILED: {e}")
        return False
    print(f"      done in {time.time() - t0:.0f}s")
    return True


def main() -> None:
    reclassify_ok = _warm_reclassify_model()
    embed_ok = _warm_embed_model()

    if reclassify_ok and embed_ok:
        print("\nBoth model caches warm — main.py once / webui.py will hit local cache, not the network.")
    else:
        print("\nAt least one model failed to cache — see FAILED lines above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
