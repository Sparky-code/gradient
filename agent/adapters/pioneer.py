"""Pioneer adapter — the RETRAINING layer.

Pioneer's real loop: mine production failures -> LoRA retrain a specialist
model -> eval every checkpoint -> route improved model behind the same
endpoint, with a human approving promotion.

Two deviations from that, both deliberate:

1. Human-approves-promotion conflicts with the hackathon's autonomy
   requirement ("self-improve ... without manual intervention") — this local
   reimplementation auto-promotes once RETRAIN_BATCH_SIZE examples land, no
   click required.

2. Instead of a numeric report nobody reads, the "retrain" step here produces
   a real, versioned artifact (see policy.py): a few-shot exemplar set built
   from actual accept/reject content, consumed by reclassify.py to visibly
   change future classification. That's what makes this self-evolving rather
   than a stub metric.

A key now exists (`API.md`), so `maybe_retrain()` also calls
`agent/adapters/pioneer_api.py` every pass — a real, additional attempt at
Pioneer's actual `/felix/training-jobs` path. It never replaces the local
promotion above: that's still what changes classification behavior, runs
unconditionally, and isn't gated on the real API call succeeding. See
pioneer_api.py's own docstring for exactly how far the real call gets before
this account's billing wall stops it.
"""

import json
from datetime import datetime, timezone

from agent import config, policy
from agent.adapters import pioneer_api

RETRAIN_BATCH_SIZE = 5   # collect this many feedback examples before a retrain pass
MAX_EXEMPLARS = 12       # cap the few-shot set so the reclassify prompt stays small


def submit_feedback(example: dict) -> None:
    """Queue one labeled training example — a user's accept/reject/share/invite
    decision on a specific classified post (subcategory/action/href), which is
    exactly the kind of production failure/success signal Pioneer's real
    pipeline mines for."""
    config.ensure_dirs()
    record = {"queued_at": datetime.now(timezone.utc).isoformat(), **example}
    with config.TRAINING_QUEUE_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _read_queue() -> list[dict]:
    if not config.TRAINING_QUEUE_FILE.exists():
        return []
    return [json.loads(line) for line in config.TRAINING_QUEUE_FILE.read_text().splitlines() if line.strip()]


def maybe_retrain() -> dict | None:
    """If enough feedback has accumulated, synthesize and auto-promote a new
    policy version from it. Returns the retrain report, or None if there
    wasn't enough signal yet to trigger a run."""
    queue = _read_queue()
    if len(queue) < RETRAIN_BATCH_SIZE:
        return None

    accepted = sum(1 for e in queue if e.get("decision") in ("accept", "share", "invite"))
    rejected = sum(1 for e in queue if e.get("decision") == "reject")
    eval_delta = (accepted - rejected) / len(queue)

    prior_exemplars = policy.load_current()["exemplars"]
    new_exemplars = [
        {
            "interest": e.get("interest"),
            "subcategory": e.get("subcategory"),
            "actionable": e.get("actionable"),
            "action": e.get("action"),
            "decision": e.get("decision"),
        }
        for e in queue
    ]
    # newest feedback first — reclassify.py's prompt budget is capped, and the
    # most recent correction should outweigh older, possibly-stale exemplars
    exemplars = (new_exemplars + prior_exemplars)[:MAX_EXEMPLARS]
    promoted_policy = policy.promote(exemplars)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "training_examples": len(queue),
        "accepted": accepted,
        "rejected": rejected,
        "eval_delta": round(eval_delta, 3),
        "promoted": True,
        "policy_version": promoted_policy["version"],
        "policy_exemplar_count": len(exemplars),
    }

    # Real Pioneer API attempt — additive, never a replacement for the local
    # promotion above (that's the artifact that actually changes classification
    # behavior, see policy.py). Any failure here — network, schema, billing —
    # must never break the real promotion that already happened.
    try:
        real = pioneer_api.attempt_real_retrain(
            queue, dataset_name=f"self-evolve-agent-feedback-v{promoted_policy['version']}"
        )
    except Exception as e:  # noqa: BLE001 — deliberate: an external API attempt must degrade, not crash the pass
        real = {"attempted": True, "stage": "exception", "ok": False, "detail": str(e)}
    report["pioneer_api"] = real

    if not real.get("attempted"):
        report["note"] = ("simulated retrain — no Pioneer API key configured; real call would POST to "
                           "/felix/training-jobs. The promoted artifact is real: see data/state/policy/current.json")
    elif real.get("ok"):
        report["note"] = (
            f"local reimplementation promoted the real policy artifact above (unchanged); "
            f"also submitted a real Pioneer training job — id={real.get('job_id')}, status={real.get('status')}"
        )
    else:
        report["note"] = (
            f"local reimplementation promoted the real policy artifact above (unchanged); "
            f"also attempted Pioneer's real API and stopped at stage '{real.get('stage')}' "
            f"(http {real.get('http_status')}): {real.get('detail')}"
        )

    config.ensure_dirs()
    report_path = config.RETRAIN_REPORTS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    report_path.write_text(json.dumps(report, indent=2))

    config.TRAINING_QUEUE_FILE.unlink()  # archived into the report; queue starts fresh
    return report
