"""Subprocess entrypoint for Playwright E2E tests — NOT collected by pytest
(leading underscore). Boots the real webui.py Flask app, but pointed at a
throwaway tmp directory instead of this repo's real data/state/, and with the
heavy pipeline entry points (loop.run_once, feedback.submit_plan) and the
VectorAI DB call (vectorai.update_status) replaced with fast fakes — same
reasoning as tests/conftest.py's isolated_env/stub_* fixtures, just done by
hand since this runs in its own process rather than under pytest.

Usage: python _live_server_app.py <tmp_dir> <port>
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

tmp_dir = Path(sys.argv[1]).resolve()
port = int(sys.argv[2])

from agent import config  # noqa: E402

data_dir = tmp_dir / "data"
state_dir = data_dir / "state"
config.DATA_DIR = data_dir
config.DROP_DIR = data_dir / "drop"
config.STATE_DIR = state_dir
config.PLANS_FILE = state_dir / "plans.json"
config.PROCESSED_FILE = state_dir / "processed_files.json"
config.TRAINING_QUEUE_FILE = state_dir / "training_queue.jsonl"
config.SENSO_INGESTED_FILE = state_dir / "senso_ingested.json"
config.VECTORAI_REMEMBERED_FILE = state_dir / "vectorai_remembered.json"
config.RETRAIN_REPORTS_DIR = state_dir / "retrain_reports"
config.SESSION_LOG_FILE = state_dir / "session_log.jsonl"
config.CITED_MD_HASH_FILE = state_dir / "cited_md_last_render_hash.json"
config.CITED_MD = tmp_dir / "cited.md"
config.API_KEYS_FILE = tmp_dir / "API.md"
config.ensure_dirs()

from agent import policy, store, taxonomy  # noqa: E402

store.SNAPSHOT_DIR = state_dir / "snapshots"
store.PLANS_LOCK._path = state_dir / ".plans.lock"
policy.POLICY_DIR = state_dir / "policy"
policy.CURRENT_FILE = policy.POLICY_DIR / "current.json"
taxonomy.TAXONOMY_DIR = state_dir / "taxonomy"
taxonomy.CURRENT_FILE = taxonomy.TAXONOMY_DIR / "current.json"

from agent import feedback, loop  # noqa: E402
from agent.adapters import vectorai  # noqa: E402


def _fake_run_once():
    return {
        "new_files": [], "new_plans": 0, "low_quality_filtered": 0,
        "taxonomy_categories_promoted": 0, "retrain_report": None,
        "cancelled": False, "failed_files": [],
    }


def _fake_submit_plan(plan_id):
    with store.PLANS_LOCK:
        plans = store.load_plans()
        plan = plans[plan_id]
        plan["ready_to_submit"] = False
        store.save_plans(plans)
    return {
        "plan_id": plan_id, "tagged": len(plan["items"]), "reassigned": [],
        "taxonomy_promoted": None, "still_unassigned": 0, "cancelled": False,
    }


def _fake_update_status(hrefs, status):
    return {"updated": True, "reason": None}


loop.run_once = _fake_run_once
feedback.submit_plan = _fake_submit_plan
vectorai.update_status = _fake_update_status

import webui  # noqa: E402

if __name__ == "__main__":
    webui.app.run(host="127.0.0.1", port=port, debug=False)
