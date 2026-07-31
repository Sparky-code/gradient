#!/usr/bin/env python3
"""Runs inside self-evolve-agent's own venv (has mlx_lm) — same isolation
pattern as _tag_worker.py/_reclassify_worker.py/_taxonomy_namer_worker.py.

Judges each external search result against a CRAAP/E-E-A-T-inspired rubric —
the one part of source-quality scoring that genuinely needs judgment, not
just metadata (see agent/source_quality.py's module docstring for why: no
automatable API exists for "is this page actually credible," so an LLM
reading the page content is the closest automatable stand-in). One model
load for the whole batch, same batching discipline as every other worker.
"""

import json
import re
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

MODEL_ID = "mlx-community/Qwen3-30B-A3B-4bit"


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    data = json.loads(Path(in_path).read_text())
    results = data["results"]

    from mlx_lm import generate, load
    model, tokenizer = load(MODEL_ID, adapter_path=data.get("adapter_path"))

    scores = []
    for result in results:
        content = result.get("raw_content") or result.get("excerpt") or ""
        prompt_text = f"""Judge whether this page is a credible, informational source for someone
trying to learn about a topic — not whether you agree with its claims, just whether it READS
like a credible source: an identifiable author or organization, first-hand or expert-level
detail rather than vague generalities, an informational purpose rather than pure advertising/
promotion, and claims that are appropriately specific rather than sweeping and unsupported.

url: {result.get('url')}
title: {result.get('title')}
content: {content[:2000]}

Respond with ONLY a single JSON object, no prose, no markdown fences:
{{"score": <float 0.0-1.0>, "reason": <short string, one sentence>}}

JSON:"""

        prompt = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt_text}],
            tokenize=False, add_generation_prompt=True, enable_thinking=False,
        )
        raw = generate(model, tokenizer, prompt=prompt, max_tokens=150, verbose=False)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        try:
            start, end = raw.index("{"), raw.rindex("}")
            parsed = json.loads(raw[start:end + 1])
            score = max(0.0, min(1.0, float(parsed.get("score", 0.0))))
            reason = str(parsed.get("reason") or "").strip() or None
        except (ValueError, KeyError, json.JSONDecodeError, TypeError):
            score, reason = None, None

        scores.append({"score": score, "reason": reason})

    Path(out_path).write_text(json.dumps(scores))


if __name__ == "__main__":
    main()
