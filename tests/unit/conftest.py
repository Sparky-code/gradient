import threading

import pytest

import webui
from agent import cancellation


@pytest.fixture(autouse=True)
def _reset_cancellation():
    """agent.cancellation holds a module-global threading.Event — not reached
    by isolated_env's path patching, so it must be reset around every test
    that might touch /cancel or a cancellable pass."""
    cancellation.reset()
    yield
    cancellation.reset()


@pytest.fixture
def client(isolated_env, stub_vectorai, monkeypatch):
    """Flask test client with fresh, isolated background-task state.

    webui's _run_state/_submit_state/_run_lock/_submit_lock are module
    globals that persist for the life of the process — without resetting
    them, a lock left held (or a stale last_result/last_error) by one test
    would leak into the next."""
    monkeypatch.setattr(webui, "_run_lock", threading.Lock())
    monkeypatch.setattr(webui, "_submit_lock", threading.Lock())
    monkeypatch.setattr(webui, "_run_state", {"running": False, "last_result": None, "last_error": None})
    monkeypatch.setattr(webui, "_submit_state",
                         {"running": False, "plan_id": None, "last_result": None, "last_error": None})
    webui.app.testing = True
    return webui.app.test_client()


@pytest.fixture
def stub_run_once(monkeypatch):
    """Replace the real ingest->plan->publish->retrain pipeline (heavy: local
    LLM subprocesses, VectorAI, Senso) with a fast fake, and record calls."""
    calls = []

    def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "new_files": [], "new_plans": 0, "low_quality_filtered": 0,
            "taxonomy_categories_promoted": 0, "retrain_report": None,
            "cancelled": False, "failed_files": [],
        }

    monkeypatch.setattr(webui.loop, "run_once", _fake)
    return calls


@pytest.fixture
def stub_submit_plan(monkeypatch):
    """Replace the real tag/reassign/new-category pass (heavy: local LLM
    subprocesses) with a fast fake, and record calls."""
    calls = []

    def _fake(plan_id):
        calls.append(plan_id)
        return {
            "plan_id": plan_id, "tagged": 0, "reassigned": [],
            "taxonomy_promoted": None, "still_unassigned": 0, "cancelled": False,
        }

    monkeypatch.setattr(webui.feedback, "submit_plan", _fake)
    return calls
