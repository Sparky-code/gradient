#!/usr/bin/env python3
"""Runs inside self-evolve-agent's own venv (has mlx_lm). Loads the
already-cached Qwen3-8B-8bit model and re-evaluates each post's category
using the current policy's few-shot exemplars — real in-context learning
off real accept/reject feedback, not a stub.

Invoked via subprocess from reclassify.py so main.py itself has no hard
mlx_lm dependency and this stays isolated from InstaGone's own venv/model.
"""

import json
import re
import sys
from pathlib import Path

# Qwen3-8B was tested first and reliably contradicted its own stated reasoning —
# e.g. "does not match any BAD example" followed by should_surface: false. Matches
# InstaGone's own RFC: 8B/9B is for fast iteration only, not decisions that gate output.
MODEL_ID = "mlx-community/Qwen3-30B-A3B-4bit"


def build_exemplar_block(exemplars: list[dict]) -> str:
    lines = []
    for e in exemplars:
        verdict = "GOOD match — keep this kind of categorization" if e["decision"] in (
            "accept", "share", "invite") else "BAD match — the category/grouping was wrong, be more skeptical here"
        lines.append(
            f'- interest="{e.get("interest")}" subcategory="{e.get("subcategory")}" '
            f'action="{e.get("action")}" -> user feedback: {e["decision"]} ({verdict})'
        )
    return "\n".join(lines)


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    data = json.loads(Path(in_path).read_text())
    posts, policy = data["posts"], data["policy"]

    from mlx_lm import generate, load
    model, tokenizer = load(MODEL_ID, adapter_path=data.get("adapter_path"))

    exemplar_block = build_exemplar_block(policy["exemplars"])
    results = []

    for post in posts:
        prompt_text = f"""You are deciding whether to surface a post to the user, based on real feedback
they gave on similar posts before. You are NOT choosing or changing this post's category — only
whether it's worth surfacing at all.

Past user feedback on similar posts (most recent first):
{exemplar_block}

Now judge this post, which is already categorized as "{post.get('category')}":
subcategory: {post.get('subcategory')}
proposed action: {post.get('action')}

Only set should_surface to false if THIS post's subcategory/action is genuinely similar in kind
to a specific BAD example above (same category AND a similar style of subcategory/action) — not
merely because it shares a broad theme. If in doubt, or if this post doesn't clearly resemble
any BAD example, should_surface must be true and confidence must be "low".
Respond with ONLY a single JSON object, no prose, no markdown fences:
{{"confidence": "high" or "low", "should_surface": true or false, "reasoning": string}}

JSON:"""

        prompt = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt_text}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        raw = generate(model, tokenizer, prompt=prompt, max_tokens=300, verbose=False)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        try:
            start, end = raw.index("{"), raw.rindex("}")
            parsed = json.loads(raw[start:end + 1])
        except (ValueError, json.JSONDecodeError):
            parsed = {"confidence": "low", "should_surface": True, "reasoning": "policy parse failed"}

        merged = dict(post)
        merged["policy_confidence"] = parsed.get("confidence", "low")
        merged["should_surface"] = parsed.get("should_surface", True)
        merged["policy_reasoning"] = parsed.get("reasoning", "")
        results.append(merged)

    Path(out_path).write_text(json.dumps(results))


if __name__ == "__main__":
    main()
