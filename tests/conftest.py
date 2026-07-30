"""Shared fixtures for the Flask-level (unit) test suite.

Every test that touches the app's file-backed state uses `isolated_env` so it
never reads or writes this repo's real data/state/ or cited.md. Several
modules compute a path once at import time from agent.config (store.SNAPSHOT_DIR,
policy.POLICY_DIR/CURRENT_FILE, taxonomy.TAXONOMY_DIR/CURRENT_FILE, and
store.PLANS_LOCK's underlying lock-file path) — patching agent.config alone
doesn't reach those, so isolated_env patches each of them directly too.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent import config, export_types, exporter, policy, profile, source_trust, store, taxonomy  # noqa: E402
from agent.adapters import vectorai  # noqa: E402


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"

    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DROP_DIR", data_dir / "drop")
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "PLANS_FILE", state_dir / "plans.json")
    monkeypatch.setattr(config, "PROCESSED_FILE", state_dir / "processed_files.json")
    monkeypatch.setattr(config, "TRAINING_QUEUE_FILE", state_dir / "training_queue.jsonl")
    monkeypatch.setattr(config, "VECTORAI_REMEMBERED_FILE", state_dir / "vectorai_remembered.json")
    monkeypatch.setattr(config, "RETRAIN_REPORTS_DIR", state_dir / "retrain_reports")
    monkeypatch.setattr(config, "SESSION_LOG_FILE", state_dir / "session_log.jsonl")
    monkeypatch.setattr(config, "CITED_MD_HASH_FILE", state_dir / "cited_md_last_render_hash.json")
    monkeypatch.setattr(config, "CITED_MD", tmp_path / "cited.md")
    monkeypatch.setattr(config, "API_KEYS_FILE", tmp_path / "API.md")

    monkeypatch.setattr(store, "SNAPSHOT_DIR", state_dir / "snapshots")
    monkeypatch.setattr(store.PLANS_LOCK, "_path", state_dir / ".plans.lock")
    monkeypatch.setattr(policy, "POLICY_DIR", state_dir / "policy")
    monkeypatch.setattr(policy, "CURRENT_FILE", state_dir / "policy" / "current.json")
    monkeypatch.setattr(taxonomy, "TAXONOMY_DIR", state_dir / "taxonomy")
    monkeypatch.setattr(taxonomy, "CURRENT_FILE", state_dir / "taxonomy" / "current.json")
    monkeypatch.setattr(export_types, "EXPORT_TYPES_DIR", state_dir / "export_types")
    monkeypatch.setattr(export_types, "CURRENT_FILE", state_dir / "export_types" / "current.json")
    monkeypatch.setattr(profile, "PROFILE_DIR", state_dir / "profile")
    monkeypatch.setattr(profile, "CURRENT_FILE", state_dir / "profile" / "current.json")
    monkeypatch.setattr(source_trust, "TRUST_DIR", state_dir / "source_trust")
    monkeypatch.setattr(source_trust, "CURRENT_FILE", state_dir / "source_trust" / "current.json")
    monkeypatch.setattr(exporter, "EXPORTS_DIR", state_dir / "exports")
    monkeypatch.setattr(exporter, "EXPORTS_MANIFEST_FILE", state_dir / "exports" / "manifest.json")
    monkeypatch.setattr(exporter, "PLAYLIST_CSV", state_dir / "exports" / "playlist.csv")
    monkeypatch.setattr(exporter, "PLACES_CSV", state_dir / "exports" / "places.csv")
    monkeypatch.setattr(exporter, "RECIPES_DIR", state_dir / "exports" / "recipes")
    monkeypatch.setattr(exporter, "SHOPPING_LIST_MD", state_dir / "exports" / "shopping_list.md")

    config.ensure_dirs()
    return {"tmp_path": tmp_path, "data_dir": data_dir, "state_dir": state_dir}


@pytest.fixture
def stub_vectorai(monkeypatch):
    """VectorAI DB is a separate Docker service — real tests shouldn't depend
    on it being up. Stub the one call path feedback.py hits (update_status)
    and record invocations for assertions."""
    calls = []

    def _fake_update_status(hrefs, status):
        calls.append({"hrefs": list(hrefs), "status": status})
        return {"updated": True, "reason": None}

    monkeypatch.setattr(vectorai, "update_status", _fake_update_status)
    return calls


