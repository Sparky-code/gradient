"""Renders cited.md — the agent's real, published, cited output — from plan state.

cited.md is a *derived view*, not a data source — plans.json is the source of
truth and every render() call fully regenerates cited.md from it. A hand-edit
made to cited.md between passes therefore still gets overwritten on the next
render(); that can't change without either making cited.md an input the loop
reads back from (which it deliberately isn't — plans.json already is that,
and item feedback already flows through it) or skipping regeneration entirely
(which would leave cited.md stale, worse than being overwritten). What *can*
change, and does here: a hand-edit is no longer silently lost. render() hashes
the previous output and compares it to what's actually on disk right before
overwriting — a mismatch means someone (or something) touched the file outside
this pipeline since the last render, and that gets logged as a real session-log
event (with the edited content's own hash, so it's identifiable) rather than
disappearing with no trace. Recovery is unchanged: store.snapshot() already
runs before every render()-triggering pass, so the hand-edited version is
sitting in data/state/snapshots/ either way — this just makes it obvious that
snapshot is the one worth restoring, instead of finding out by accident.
"""

import hashlib
import json
from datetime import datetime, timezone

from agent import config, session_log, store


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_for_hand_edit() -> None:
    """Compare the on-disk cited.md against the hash we recorded after the
    last render() — a mismatch means it was edited outside the pipeline since
    then. Best-effort: a missing hash file (first-ever render, or an older
    data/state/ predating this check) just means there's nothing to compare
    against, not an error."""
    if not config.CITED_MD.exists() or not config.CITED_MD_HASH_FILE.exists():
        return
    last_known = json.loads(config.CITED_MD_HASH_FILE.read_text()).get("sha256")
    current_text = config.CITED_MD.read_text()
    current_hash = _sha256(current_text)
    if last_known and current_hash != last_known:
        session_log.log_session({
            "event": "cited_md_hand_edit_overwritten",
            "detected_hash": current_hash,
            "note": (
                "cited.md changed outside the pipeline since the last render — "
                "about to be regenerated from plans.json. The edited version is "
                "preserved in this pass's store.snapshot() (see data/state/snapshots/)."
            ),
        })


def _record_render_hash(text: str) -> None:
    config.ensure_dirs()
    config.CITED_MD_HASH_FILE.write_text(json.dumps({"sha256": _sha256(text)}))

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
            lines.append("  - _Grounded citations (other posts you saved):_")
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
    _check_for_hand_edit()
    config.CITED_MD.write_text(text)
    _record_render_hash(text)
    return text
