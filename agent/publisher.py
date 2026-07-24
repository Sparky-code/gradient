"""Renders cited.md — the agent's real, published, cited output — from plan state."""

from datetime import datetime, timezone

from agent import config, store
from agent.adapters import payments

STATUS_EMOJI = {
    "pending": "⏳",
    "accepted": "✅",
    "rejected": "❌",
    "mixed": "\U0001f500",
    "shared": "\U0001f4e4",
    "invited": "\U0001f465",
}


def render(low_quality_count: int = 0) -> str:
    plans = list(store.load_plans().values())
    plans.sort(key=lambda p: len(p["items"]), reverse=True)

    lines = [
        "# cited.md",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat()} — {len(plans)} interest plan(s), "
        f"{low_quality_count} low-signal post(s) filtered out._",
        "",
        payments.PAYWALL_NOTICE,
        "",
    ]

    for plan in plans:
        emoji = STATUS_EMOJI.get(plan["status"], "")
        lines.append(f"## {emoji} {plan['interest']} — `{plan['plan_id']}` ({plan['status']})")
        lines.append("")
        for item in plan["items"]:
            item_status = item.get("status", "pending")
            item_emoji = STATUS_EMOJI.get(item_status, "")
            tags = item.get("tags") or []
            tags_suffix = f" _[{', '.join(tags)}]_" if tags else ""
            lines.append(
                f"- {item_emoji} **{item['subcategory']}** ({item['actionable']}, {item_status}){tags_suffix}: "
                f"{item['action']}"
            )
            for fact in item.get("key_facts") or []:
                lines.append(f"  - {fact}")
            lines.append(f"  - source: <{item['href']}>")
        grounding = plan.get("grounding")
        if grounding and grounding.get("grounded"):
            lines.append("  - _Senso-grounded citations:_")
            for c in grounding["citations"]:
                lines.append(f"    - {c}")
        memory = plan.get("memory")
        if memory and memory.get("recalled"):
            lines.append("  - \U0001f9e0 _VectorAI DB recall — similar past posts:_")
            for m in memory["memories"]:
                emoji = STATUS_EMOJI.get(m.get("status"), "")
                lines.append(
                    f"    - {emoji} `{m.get('status')}` ({m.get('score')}): "
                    f"{m.get('subcategory')} — {m.get('action')}"
                )
        lines.append("")
        lines.append(
            "  Respond per item from the dashboard (Accept/Reject), or bulk-decide the whole plan via CLI: "
            f"`python main.py feedback {plan['plan_id']} <accept|reject|share|invite>`"
        )
        lines.append("")

    text = "\n".join(lines)
    config.CITED_MD.write_text(text)
    return text
