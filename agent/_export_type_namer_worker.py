#!/usr/bin/env python3
"""Runs inside self-evolve-agent's own venv (has mlx_lm) — same isolation
pattern as _taxonomy_namer_worker.py, whose prompt/parsing shape this
mirrors closely. Given a real cluster of actionable posts that already
cleared two independent bars (VectorAI DB found a genuine semantic cluster;
the same cluster does NOT match any already-promoted emergent export type —
see export_type_evolver.py), proposes a short export-type name + description.

Unlike the taxonomy namer (which proposes a topic category in "x and y"
style), this proposes the *kind of actionable output* a person would want
from this content — a short noun phrase like "book recommendations" or
"workout routines" — since that's what agent/exporter.py will use as a file
name and section header, not a classification label.
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
    cluster_posts, citations, existing_types = (
        data["cluster_posts"], data["citations"], data["existing_types"],
    )

    from mlx_lm import generate, load
    model, tokenizer = load(MODEL_ID)

    posts_block = "\n".join(
        f'- subcategory="{p.get("subcategory")}" action="{p.get("action")}"' for p in cluster_posts
    )
    citations_block = "\n".join(f"- {c}" for c in citations) or "(none)"

    prompt_text = f"""A recurring group of saved, actionable posts doesn't match any export type below.
Propose ONE new export type name for the kind of structured output this content should become.

Existing export types (do NOT propose anything close in meaning to these — a new
type only makes sense if the actionable content below is genuinely distinct):
{", ".join(existing_types)}

The recurring posts:
{posts_block}

Independently confirmed real-world context for this topic (from a knowledge base search):
{citations_block}

Respond with ONLY a single JSON object, no prose, no markdown fences:
{{"type": string (short lowercase noun phrase describing the kind of actionable output, e.g. "book recommendations"), "description": string (one sentence, what this export type actually covers)}}

JSON:"""

    prompt = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt_text}],
        tokenize=False, add_generation_prompt=True, enable_thinking=False,
    )
    raw = generate(model, tokenizer, prompt=prompt, max_tokens=200, verbose=False)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

    try:
        start, end = raw.index("{"), raw.rindex("}")
        parsed = json.loads(raw[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        parsed = {"type": None, "description": None}

    Path(out_path).write_text(json.dumps(parsed))


if __name__ == "__main__":
    main()
