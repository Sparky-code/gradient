"""Plan state persistence — the single source of truth cited.md is rendered from.

Merging by plan_id lets repeated loop iterations accumulate new items into an
existing interest's plan without clobbering a status the user already set
(accept/reject/share/invite survives new posts arriving in the same category).

PLANS_LOCK exists because of a real, reproduced bug: reevaluator.py used to
hold a plans.json snapshot in memory across its entire slow tag/reassign pass
(30-60+s of local-model calls), then overwrote the whole file with that stale
snapshot at the end — silently discarding any other write that landed on a
*different* plan during that window (an item accept/reject click, a second
plan submission, a full run). "Submit one plan, every other plan reverts to
how it looked a minute ago" is exactly what a stale full-dict overwrite looks
like. Every caller that does load-mutate-save against plans.json must hold
this lock for that whole sequence — see reevaluator.py for the pattern that
keeps the *slow* work (tagging, embedding, grounding) outside the lock and
only the fast final read-modify-write inside it.

PLANS_LOCK also holds a real cross-process file lock (fcntl.flock), not just
an in-process threading.Lock — a second *process* (e.g. `main.py once` run
from a terminal while webui.py is also live, or two `loop` invocations)
doing its own load-mutate-save against plans.json is exactly the same race,
just across the process boundary where threading.Lock can't reach. A second
process now blocks on the flock until the first releases it, rather than
interleaving reads/writes.
"""

import fcntl
import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from agent import config
from agent.adapters import vectorai


class _ProcessLock:
    """A threading.Lock() plus an fcntl.flock() on a dedicated lock file, so
    `with PLANS_LOCK:` serializes both other threads in this process and any
    other process touching the same data/state/ directory. The thread lock is
    acquired first (cheap, and guarantees only one thread in this process ever
    holds the fd/flock at a time) and released last."""

    def __init__(self, path: Path):
        self._path = path
        self._thread_lock = threading.Lock()
        self._fd = None

    def __enter__(self) -> "_ProcessLock":
        self._thread_lock.acquire()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self._path, "w")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc_info) -> None:
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()
        self._fd = None
        self._thread_lock.release()


PLANS_LOCK = _ProcessLock(config.STATE_DIR / ".plans.lock")

SNAPSHOT_DIR = config.STATE_DIR / "snapshots"
MAX_SNAPSHOTS = 10


def load_plans() -> dict[str, dict]:
    if not config.PLANS_FILE.exists():
        return {}
    return {p["plan_id"]: p for p in json.loads(config.PLANS_FILE.read_text())}


def save_plans(plans_by_id: dict[str, dict]) -> None:
    config.ensure_dirs()
    config.PLANS_FILE.write_text(json.dumps(list(plans_by_id.values()), indent=2))


def rollup_status(items: list[dict]) -> str:
    """Plan-level status derived from per-item decisions — shared by
    feedback.py (recomputing a plan's status as items change) and
    reevaluator.py (recomputing it after dropping the rejected half of a
    mixed plan)."""
    statuses = {item.get("status", "pending") for item in items}
    if statuses <= {"accepted"}:
        return "accepted"
    if statuses <= {"rejected"}:
        return "rejected"
    if "pending" in statuses:
        return "pending"
    return "mixed"


def merge_plans(new_plans: list[dict]) -> dict[str, dict]:
    with PLANS_LOCK:
        existing = load_plans()
        seen_hrefs = {
            plan_id: {item["href"] for item in plan["items"]}
            for plan_id, plan in existing.items()
        }

        for plan in new_plans:
            plan_id = plan["plan_id"]
            if plan_id not in existing:
                existing[plan_id] = plan
                continue

            already = seen_hrefs.get(plan_id, set())
            fresh_items = [item for item in plan["items"] if item["href"] not in already]
            existing[plan_id]["items"].extend(fresh_items)
            existing[plan_id]["generated_at"] = plan["generated_at"]

        save_plans(existing)
        return existing


def _snapshot_targets() -> dict[str, Path]:
    return {
        "plans.json": config.PLANS_FILE,
        "cited.md": config.CITED_MD,
        "policy_current.json": config.STATE_DIR / "policy" / "current.json",
        "taxonomy_current.json": config.STATE_DIR / "taxonomy" / "current.json",
        "export_types_current.json": config.STATE_DIR / "export_types" / "current.json",
        "profile_current.json": config.STATE_DIR / "profile" / "current.json",
        "source_trust_current.json": config.STATE_DIR / "source_trust" / "current.json",
    }


def snapshot(label: str) -> str:
    """Copy the current plans/cited.md/policy/taxonomy state into a timestamped
    folder before a risky automated pass (run_once, reevaluate_plan) — the
    only backup that exists anywhere in this system otherwise. Best-effort per
    file (a missing source, e.g. no policy promoted yet, is just skipped) so a
    partial system state still gets whatever backup is possible. Reuses
    PLANS_LOCK rather than a dedicated lock — every writer of these files
    already serializes through it (see PLANS_LOCK's docstring above), so this
    keeps snapshot-taking in that same order rather than adding a second lock
    to reason about."""
    with PLANS_LOCK:
        snap_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        dest = SNAPSHOT_DIR / snap_id
        dest.mkdir(parents=True, exist_ok=True)
        for name, src in _snapshot_targets().items():
            if src.exists():
                shutil.copy2(src, dest / name)
        (dest / "meta.json").write_text(json.dumps({
            "label": label, "created_at": datetime.now(timezone.utc).isoformat(),
        }))

    snaps = sorted(SNAPSHOT_DIR.iterdir(), key=lambda p: p.name, reverse=True)
    for stale in snaps[MAX_SNAPSHOTS:]:
        shutil.rmtree(stale, ignore_errors=True)
    return snap_id


def list_snapshots() -> list[dict]:
    if not SNAPSHOT_DIR.exists():
        return []
    out = []
    for d in sorted(SNAPSHOT_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        meta_file = d / "meta.json"
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        out.append({"id": d.name, "label": meta.get("label", ""), "created_at": meta.get("created_at", "")})
    return out


def restore_snapshot(snapshot_id: str) -> None:
    """Restore plans/cited.md/policy/taxonomy state from a prior snapshot() —
    exact restore, including removing a file that didn't exist yet at
    snapshot time (e.g. resetting to before the first policy was promoted)."""
    src_dir = SNAPSHOT_DIR / snapshot_id
    if not src_dir.exists():
        raise KeyError(f"no such snapshot: {snapshot_id}")
    with PLANS_LOCK:
        for name, dest in _snapshot_targets().items():
            src = src_dir / name
            if src.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            elif dest.exists():
                dest.unlink()


def full_reset() -> dict:
    """Wipes ALL local state back to a fresh-install baseline, for running a
    new experiment from scratch — not `/reset`'s "undo the last pass" (that
    only restores the most recent snapshot). Clears: plans, cited.md, every
    promoted policy/taxonomy/export-type/profile/source-trust artifact,
    ingest bookkeeping (processed-files list, VectorAI-remembered hrefs,
    training queue, retrain reports), the session log, every generated
    export, and every VectorAI DB collection this project owns
    (agent.adapters.vectorai.clear_all_collections()) — so stale embeddings
    from the prior experiment don't bias recall/grounding/clustering on the
    next pass. Every module that owns one of these files bootstraps cleanly
    from an empty/missing state already (policy.py starts at version 0,
    taxonomy.py/export_types.py reseed on the next real pass) — this doesn't
    need to know any of their internals beyond "delete the directory."

    Takes a snapshot first ("before full reset") — the one thing this does
    NOT wipe is snapshot history itself, so triggering this by mistake is
    still recoverable via the regular /reset, same as any other risky pass.

    data/drop/'s uploaded export files are left untouched — deleting
    processed_files.json (not the drop files themselves) means the very
    next run picks the same input back up as if it were new, which is the
    actual "iterate through the workflow" use case this exists for; a
    person who wants genuinely different input still just uploads a new
    file same as ever."""
    snapshot("before full reset")

    with PLANS_LOCK:
        for f in (
            config.PLANS_FILE, config.CITED_MD, config.PROCESSED_FILE,
            config.TRAINING_QUEUE_FILE, config.VECTORAI_REMEMBERED_FILE,
            config.SESSION_LOG_FILE, config.CITED_MD_HASH_FILE,
        ):
            f.unlink(missing_ok=True)

        for d in (
            config.STATE_DIR / "policy", config.STATE_DIR / "taxonomy",
            config.STATE_DIR / "export_types", config.STATE_DIR / "profile",
            config.STATE_DIR / "source_trust", config.RETRAIN_REPORTS_DIR,
        ):
            shutil.rmtree(d, ignore_errors=True)

        from agent import exporter  # deferred: exporter.py imports store.py
        shutil.rmtree(exporter.EXPORTS_DIR, ignore_errors=True)

        config.ensure_dirs()

    return {"vectorai": vectorai.clear_all_collections()}
