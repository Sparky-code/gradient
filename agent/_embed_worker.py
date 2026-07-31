#!/usr/bin/env python3
"""Runs inside self-evolve-agent's own venv (has torch + transformers).
Loads the already-cached nomic-embed-text-v1.5 model and embeds a batch of
texts in one process — real local embeddings, no API key, no network call
at runtime (HF_HUB_OFFLINE=1 forces this to fail loudly instead of silently
hitting the network if the cache is ever missing).

Invoked via subprocess from agent/adapters/vectorai.py so main.py itself
has no hard torch dependency — same isolation pattern as
_reclassify_worker.py/mlx_lm.

nomic-embed-text-v1.5 requires a task prefix ("search_document: " for
stored content, "search_query: " for queries) and supports Matryoshka
truncation, so callers pass {"texts": [...], "kind": "document"|"query",
"dim": int}.
"""

import json
import os
import sys
from pathlib import Path

# Running this file as a script puts agent/ itself on sys.path[0], where its
# own modules shadow same-named stdlib ones. agent/profile.py in particular
# shadows stdlib `profile`, which cProfile imports, which torch/transformers
# and mlx_lm pull in transitively — and agent/profile.py's own `from agent
# import ...` then fails, because agent/ is on the path but the repo root
# holding the `agent` package is not. transformers reports that as the
# spectacularly unhelpful "Could not import module 'AutoModel'", and
# vectorai.embed_batch() turns it into a silent None, which every caller
# treats as "no embeddings this pass". Net effect: the whole vector layer
# (grounding, recall, clustering, reassignment) degrades to a no-op with no
# error anywhere. Point sys.path[0] at the repo root instead — the stdlib
# wins again, and `agent` stays importable for the workers that want it.
sys.path[0] = str(Path(__file__).resolve().parents[1])

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

MODEL_ID = "nomic-ai/nomic-embed-text-v1.5"
PREFIX = {"document": "search_document: ", "query": "search_query: "}


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    data = json.loads(Path(in_path).read_text())
    texts, kind, dim = data["texts"], data["kind"], data["dim"]

    import torch
    import torch.nn.functional as F
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    model = AutoModel.from_pretrained(MODEL_ID, trust_remote_code=True)
    model.eval()

    prefixed = [PREFIX[kind] + t for t in texts]
    encoded = tokenizer(prefixed, padding=True, truncation=True, return_tensors="pt")
    with torch.no_grad():
        output = model(**encoded)

    token_embeddings = output[0]
    mask = encoded["attention_mask"].unsqueeze(-1).expand(token_embeddings.size()).float()
    pooled = torch.sum(token_embeddings * mask, 1) / torch.clamp(mask.sum(1), min=1e-9)
    pooled = F.layer_norm(pooled, normalized_shape=(pooled.shape[1],))
    truncated = pooled[:, :dim]
    normalized = F.normalize(truncated, p=2, dim=1)

    Path(out_path).write_text(json.dumps(normalized.tolist()))


if __name__ == "__main__":
    main()
