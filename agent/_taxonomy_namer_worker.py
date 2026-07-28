#!/usr/bin/env python3
"""Runs inside self-evolve-agent's own venv (has mlx_lm) — same isolation pattern
as _reclassify_worker.py. Given a real cluster of "other"-bucketed posts that
already cleared two independent bars (VectorAI DB found a genuine semantic
cluster; the same cluster does NOT match any existing category — see
taxonomy_evolver.py), proposes a short category name + one-line description.

Deliberately NOT given the freedom to invent wild names: it only sees the
cluster's real subcategory/action text plus real grounding citations (other
posts the user saved, via VectorAI DB's own local search), and is instructed
to output a name in the same lowercase "x and y" style as the
existing taxonomy so a promoted category reads like it belongs, not like a
one-off label.
"""

import json
import re
import sys
from pathlib import Path

MODEL_ID = "mlx-community/Qwen3-30B-A3B-4bit"


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    data = json.loads(Path(in_path).read_text())
    cluster_posts, citations, existing_categories = (
        data["cluster_posts"], data["citations"], data["existing_categories"],
    )

    from mlx_lm import generate, load
    model, tokenizer = load(MODEL_ID, adapter_path=data.get("adapter_path"))

    posts_block = "\n".join(
        f'- subcategory="{p.get("subcategory")}" action="{p.get("action")}"' for p in cluster_posts
    )
    citations_block = "\n".join(f"- {c}" for c in citations) or "(none)"

    prompt_text = f"""A recurring group of saved posts doesn't fit any existing category below.
Propose ONE new category name for this real, recurring topic.

Existing categories (do NOT propose anything close in meaning to these — a new
category only makes sense if the topic below is genuinely distinct):
{", ".join(existing_categories)}

The recurring posts:
{posts_block}

Independently confirmed real-world context for this topic (from a knowledge base search):
{citations_block}

Respond with ONLY a single JSON object, no prose, no markdown fences:
{{"category": string (lowercase, "x and y" style matching the existing categories' naming convention), "description": string (one sentence, what this category actually covers)}}

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
        parsed = {"category": None, "description": None}

    Path(out_path).write_text(json.dumps(parsed))


if __name__ == "__main__":
    main()
