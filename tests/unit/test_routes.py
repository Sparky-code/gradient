"""Flask-test-client coverage of every route webui.py exposes (see its own
module docstring's "Routes:" list). Heavy pipeline entry points (loop.run_once,
feedback.submit_plan) are stubbed via stub_run_once/stub_submit_plan so these
stay fast and don't require Docker/local LLM models; feedback.record_item and
publisher.render() run for real since they're cheap, local, and are exactly
the state-transition logic worth exercising directly.
"""

import json

from agent import cancellation, config, store

from tests.helpers import make_item, make_plan, wait_until, write_plans


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------

def test_dashboard_empty_state(client, isolated_env):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"No plans yet" in resp.data


def test_dashboard_renders_plan_and_items(client, isolated_env):
    write_plans(make_plan(items=[make_item(subcategory="hiking trails", action="go hiking")]))
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"test interest" in resp.data
    assert b"hiking trails" in resp.data
    assert b"go hiking" in resp.data


def test_dashboard_shows_processing_view_while_running(client, isolated_env):
    client.application  # noqa: B018 - touch fixture
    import webui
    webui._run_state["running"] = True
    resp = client.get("/")
    assert b"Running\xe2\x80\xa6" in resp.data or b"Running" in resp.data
    assert b'<meta http-equiv="refresh" content="5">' in resp.data


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------

def test_trigger_run_starts_background_pass_and_redirects(client, isolated_env, stub_run_once):
    import webui
    resp = client.post("/run")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    assert wait_until(lambda: not webui._run_state["running"])
    assert len(stub_run_once) == 1
    assert webui._run_state["last_result"]["new_plans"] == 0
    assert webui._run_state["last_error"] is None


def test_trigger_run_while_already_running_is_a_noop(client, isolated_env, stub_run_once):
    import webui
    assert webui._run_lock.acquire(blocking=False)
    try:
        client.post("/run")
    finally:
        webui._run_lock.release()
    assert len(stub_run_once) == 0


def test_run_failure_is_captured_as_last_error_not_a_crash(client, isolated_env, monkeypatch):
    import webui

    def _boom():
        raise RuntimeError("ingest exploded")

    monkeypatch.setattr(webui.loop, "run_once", _boom)
    resp = client.post("/run")
    assert resp.status_code == 302
    assert wait_until(lambda: not webui._run_state["running"])
    assert webui._run_state["last_error"] == "ingest exploded"


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------

def test_upload_json_file_saves_and_triggers_run(client, isolated_env, stub_run_once):
    import io
    data = {"export_file": (io.BytesIO(b'{"posts": []}'), "export.json")}
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert (config.DROP_DIR / "export.json").exists()
    import webui
    assert wait_until(lambda: len(stub_run_once) == 1)


def test_upload_rejects_non_json_filename(client, isolated_env, stub_run_once):
    import io
    data = {"export_file": (io.BytesIO(b"not json"), "export.txt")}
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert not (config.DROP_DIR / "export.txt").exists()
    assert len(stub_run_once) == 0


def test_upload_disabled_while_a_run_is_in_progress(client, isolated_env, stub_run_once):
    import io
    import webui
    webui._run_state["running"] = True
    data = {"export_file": (io.BytesIO(b'{"posts": []}'), "export.json")}
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert not (config.DROP_DIR / "export.json").exists()
    assert len(stub_run_once) == 0


# ---------------------------------------------------------------------------
# POST /feedback (per-item accept/reject)
# ---------------------------------------------------------------------------

def test_item_feedback_accept_updates_item_and_plan_status(client, isolated_env, stub_vectorai):
    item = make_item(href="https://example.com/a", status="pending")
    write_plans(make_plan(plan_id="plan-x", items=[item]))

    resp = client.post("/feedback", data={
        "plan_id": "plan-x", "href": "https://example.com/a", "decision": "accept",
    })
    assert resp.status_code == 302

    plans = store.load_plans()
    assert plans["plan-x"]["items"][0]["status"] == "accepted"
    assert plans["plan-x"]["status"] == "accepted"
    assert plans["plan-x"]["ready_to_submit"] is True
    assert stub_vectorai == [{"hrefs": ["https://example.com/a"], "status": "accepted"}]


def test_item_feedback_mixed_decisions_roll_up_to_mixed(client, isolated_env, stub_vectorai):
    write_plans(make_plan(plan_id="plan-x", items=[
        make_item(href="https://example.com/a"),
        make_item(href="https://example.com/b"),
    ]))

    client.post("/feedback", data={"plan_id": "plan-x", "href": "https://example.com/a", "decision": "accept"})
    client.post("/feedback", data={"plan_id": "plan-x", "href": "https://example.com/b", "decision": "reject"})

    plan = store.load_plans()["plan-x"]
    assert plan["status"] == "mixed"
    assert plan["ready_to_submit"] is True


def test_item_feedback_leaves_plan_pending_until_every_item_decided(client, isolated_env, stub_vectorai):
    write_plans(make_plan(plan_id="plan-x", items=[
        make_item(href="https://example.com/a"),
        make_item(href="https://example.com/b"),
    ]))

    client.post("/feedback", data={"plan_id": "plan-x", "href": "https://example.com/a", "decision": "accept"})

    plan = store.load_plans()["plan-x"]
    assert plan["status"] == "pending"
    assert plan["ready_to_submit"] is False


def test_item_feedback_publishes_cited_md(client, isolated_env, stub_vectorai):
    write_plans(make_plan(plan_id="plan-x", items=[
        make_item(href="https://example.com/a", subcategory="hiking trails"),
    ]))
    client.post("/feedback", data={"plan_id": "plan-x", "href": "https://example.com/a", "decision": "accept"})
    assert config.CITED_MD.exists()
    assert "hiking trails" in config.CITED_MD.read_text()


def test_item_feedback_logs_to_session_log(client, isolated_env, stub_vectorai):
    write_plans(make_plan(plan_id="plan-x", items=[make_item(href="https://example.com/a")]))
    client.post("/feedback", data={"plan_id": "plan-x", "href": "https://example.com/a", "decision": "accept"})

    events = [json.loads(line) for line in config.SESSION_LOG_FILE.read_text().splitlines()]
    assert any(e["event"] == "item_feedback" and e["decision"] == "accept" for e in events)


def test_feedback_unknown_plan_id_is_ignored_not_a_crash(client, isolated_env, stub_vectorai):
    resp = client.post("/feedback", data={
        "plan_id": "no-such-plan", "href": "https://example.com/a", "decision": "accept",
    })
    assert resp.status_code == 302


def test_feedback_unknown_href_is_ignored_not_a_crash(client, isolated_env, stub_vectorai):
    write_plans(make_plan(plan_id="plan-x", items=[make_item(href="https://example.com/a")]))
    resp = client.post("/feedback", data={
        "plan_id": "plan-x", "href": "https://example.com/does-not-exist", "decision": "accept",
    })
    assert resp.status_code == 302
    assert store.load_plans()["plan-x"]["items"][0]["status"] == "pending"


def test_feedback_invalid_decision_is_ignored_not_a_crash(client, isolated_env, stub_vectorai):
    write_plans(make_plan(plan_id="plan-x", items=[make_item(href="https://example.com/a")]))
    resp = client.post("/feedback", data={
        "plan_id": "plan-x", "href": "https://example.com/a", "decision": "share",
    })
    assert resp.status_code == 302
    assert store.load_plans()["plan-x"]["items"][0]["status"] == "pending"


# ---------------------------------------------------------------------------
# POST /submit-plan
# ---------------------------------------------------------------------------

def test_submit_plan_runs_reassignment_pass_when_ready(client, isolated_env, stub_submit_plan):
    write_plans(make_plan(plan_id="plan-x", status="accepted", ready_to_submit=True))
    import webui

    resp = client.post("/submit-plan", data={"plan_id": "plan-x"})
    assert resp.status_code == 302
    assert wait_until(lambda: not webui._submit_state["running"])
    assert stub_submit_plan == ["plan-x"]
    assert webui._submit_state["last_result"]["plan_id"] == "plan-x"


def test_submit_plan_while_already_submitting_is_a_noop(client, isolated_env, stub_submit_plan):
    import webui
    assert webui._submit_lock.acquire(blocking=False)
    try:
        client.post("/submit-plan", data={"plan_id": "plan-x"})
    finally:
        webui._submit_lock.release()
    assert stub_submit_plan == []


def test_submit_plan_not_ready_surfaces_as_last_error(client, isolated_env):
    """Uses the REAL feedback.submit_plan (no stub) to confirm the not-ready
    guard's ValueError is caught and surfaced, not left to crash the thread."""
    write_plans(make_plan(plan_id="plan-x", status="pending", ready_to_submit=False))
    import webui

    resp = client.post("/submit-plan", data={"plan_id": "plan-x"})
    assert resp.status_code == 302
    assert wait_until(lambda: not webui._submit_state["running"])
    assert webui._submit_state["last_error"] is not None
    assert "not fully resolved" in webui._submit_state["last_error"]


# ---------------------------------------------------------------------------
# POST /cancel
# ---------------------------------------------------------------------------

def test_cancel_sets_cancellation_flag(client, isolated_env):
    assert not cancellation.is_cancelled()
    resp = client.post("/cancel")
    assert resp.status_code == 302
    assert cancellation.is_cancelled()


# ---------------------------------------------------------------------------
# POST /reset
# ---------------------------------------------------------------------------

def test_reset_without_snapshot_is_a_noop(client, isolated_env):
    write_plans(make_plan(plan_id="plan-x"))
    resp = client.post("/reset")
    assert resp.status_code == 302
    assert "plan-x" in store.load_plans()


def test_reset_restores_most_recent_snapshot(client, isolated_env):
    write_plans(make_plan(plan_id="plan-original"))
    store.snapshot("before change")
    write_plans(make_plan(plan_id="plan-after-change"))

    import webui
    webui._run_state["last_error"] = "some stale error"
    webui._submit_state["last_result"] = {"plan_id": "stale"}

    resp = client.post("/reset")
    assert resp.status_code == 302

    plans = store.load_plans()
    assert "plan-original" in plans
    assert "plan-after-change" not in plans
    assert webui._run_state["last_error"] is None
    assert webui._submit_state["last_result"] is None


def test_reset_refuses_while_a_run_is_in_progress(client, isolated_env):
    write_plans(make_plan(plan_id="plan-original"))
    store.snapshot("before change")
    write_plans(make_plan(plan_id="plan-after-change"))

    import webui
    webui._run_state["running"] = True
    resp = client.post("/reset")
    assert resp.status_code == 302
    assert "plan-after-change" in store.load_plans()


# ---------------------------------------------------------------------------
# POST /alerts/clear
# ---------------------------------------------------------------------------

def test_clear_alerts_resets_all_alert_fields(client, isolated_env):
    import webui
    webui._run_state["last_error"] = "boom"
    webui._run_state["last_result"] = {"new_plans": 1}
    webui._submit_state["last_error"] = "boom2"
    webui._submit_state["last_result"] = {"plan_id": "x"}

    resp = client.post("/alerts/clear")
    assert resp.status_code == 302
    assert webui._run_state["last_error"] is None
    assert webui._run_state["last_result"] is None
    assert webui._submit_state["last_error"] is None
    assert webui._submit_state["last_result"] is None


# ---------------------------------------------------------------------------
# GET /cited
# ---------------------------------------------------------------------------

def test_cited_page_placeholder_when_no_output_yet(client, isolated_env):
    resp = client.get("/cited")
    assert resp.status_code == 200
    assert b"no cited.md yet" in resp.data


def test_cited_page_shows_published_content(client, isolated_env):
    write_plans(make_plan(items=[make_item(subcategory="hiking trails")]))
    from agent import publisher
    publisher.render()

    resp = client.get("/cited")
    assert resp.status_code == 200
    assert b"hiking trails" in resp.data


# ---------------------------------------------------------------------------
# GET / Exports card, GET /exports/<filename>
# ---------------------------------------------------------------------------

def test_dashboard_shows_no_exports_placeholder_when_none_generated_yet(client, isolated_env):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"No exports yet" in resp.data


def test_dashboard_shows_export_counts_and_download_links(client, isolated_env):
    item = make_item(href="https://www.instagram.com/reel/MUSIC1/", subcategory="music recommendations")
    item["entity_type"] = "music"
    item["entity_fields"] = {"tracks": [{"artist": "someartist", "track": "Some Track"}]}
    write_plans(make_plan(items=[item]))

    from agent import exporter
    exporter.render_all()

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"playlist.csv" in resp.data
    assert f'/exports/playlist.csv'.encode() in resp.data


def test_download_export_serves_file(client, isolated_env):
    from agent import exporter
    exporter.EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (exporter.EXPORTS_DIR / "playlist.csv").write_text("artist,track,href,plan_interest\n")

    resp = client.get("/exports/playlist.csv")
    assert resp.status_code == 200
    assert b"artist,track,href,plan_interest" in resp.data


def test_download_export_missing_file_returns_404(client, isolated_env):
    resp = client.get("/exports/no-such-file.csv")
    assert resp.status_code == 404
