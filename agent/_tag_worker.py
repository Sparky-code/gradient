#!/usr/bin/env python3
"""Runs inside self-evolve-agent's own venv (has mlx_lm) — same isolation pattern
as _reclassify_worker.py/_taxonomy_namer_worker.py. Generates short descriptive
tags per item, one model load for the whole batch (not one per item — the same
batching discipline as everywhere else in this codebase).

Tags are a deeper classification layer than category/subcategory: multiple
concrete, specific keywords (ingredients, techniques, named things) that
subcategory alone doesn't capture — meant to make search/filter/matching finer-
grained than the 9-ish top-level categories can be.
"""

import json
import re
import sys
from pathlib import Path

MODEL_ID = "mlx-community/Qwen3-30B-A3B-4bit"


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    items = json.loads(Path(in_path).read_text())["items"]

    from mlx_lm import generate, load
    model, tokenizer = load(MODEL_ID)

    results = []
    for item in items:
        prompt_text = f"""Generate 3-6 short, specific tags for this saved post — concrete
keywords (ingredients, techniques, named tools/places/things), not the broad category it's
already filed under.

subcategory: {item.get('subcategory')}
action: {item.get('action')}
key facts: {'; '.join(item.get('key_facts') or [])}

Respond with ONLY a single JSON object, no prose, no markdown fences:
{{"tags": [string, ...]}}

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
            tags = [str(t).strip().lower() for t in parsed.get("tags", []) if str(t).strip()]
        except (ValueError, json.JSONDecodeError):
            tags = []

        results.append(tags)

    Path(out_path).write_text(json.dumps(results))


if __name__ == "__main__":
    main()
