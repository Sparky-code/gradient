"""Flask-test-client coverage of every route webui.py exposes (see its own
module docstring's "Routes:" list). Heavy pipeline entry points (loop.run_once,
feedback.submit_plan) are stubbed via stub_run_once/stub_submit_plan so these
stay fast and don't require Docker/local LLM models; feedback.record_item and
publisher.render() run for real since they're cheap, local, and are exactly
the state-transition logic worth exercising directly.
"""

import json

from agent import cancellation, config, store, taxonomy

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


# ---------------------------------------------------------------------------
# GET /item/<href>
# ---------------------------------------------------------------------------

def test_item_detail_shows_tags_and_history(client, isolated_env):
    item = make_item(href="https://www.instagram.com/reel/AAA111/", subcategory="hiking trails", tags=["gear", "trail"])
    item["history"] = [{
        "event": "tagged", "at": "2026-01-01T00:00:00+00:00",
        "from_plan": None, "score": None, "reason": None,
    }]
    write_plans(make_plan(plan_id="plan-x", interest="outdoors", items=[item]))

    resp = client.get("/item/https://www.instagram.com/reel/AAA111/")
    assert resp.status_code == 200
    assert b"hiking trails" in resp.data
    assert b"gear" in resp.data
    assert b"tagged" in resp.data


def test_item_detail_no_history_shows_empty_state(client, isolated_env):
    write_plans(make_plan(plan_id="plan-x", items=[make_item(href="https://example.com/a")]))
    resp = client.get("/item/https://example.com/a")
    assert resp.status_code == 200
    assert b"No history yet" in resp.data


def test_item_detail_unknown_href_returns_404(client, isolated_env):
    resp = client.get("/item/https://example.com/does-not-exist")
    assert resp.status_code == 404


def test_item_detail_shows_taxonomy_evidence_when_href_in_cluster(client, isolated_env):
    href = "https://example.com/a"
    write_plans(make_plan(plan_id="plan-x", interest="new hobby", items=[make_item(href=href)]))
    taxonomy.promote("new hobby", evidence={
        "cluster_size": 3, "cluster_hrefs": [href, "https://example.com/b"],
        "description": "a genuinely new cluster of posts", "grounding_citations": ["citation one"],
        "reuse_check_cleared": True,
    })

    resp = client.get(f"/item/{href}")
    assert resp.status_code == 200
    assert b"new hobby" in resp.data
    assert b"a genuinely new cluster of posts" in resp.data
    assert b"citation one" in resp.data


def test_item_detail_taxonomy_evidence_falls_back_to_legacy_senso_citations(client, isolated_env):
    href = "https://example.com/a"
    write_plans(make_plan(plan_id="plan-x", interest="new hobby", items=[make_item(href=href)]))
    taxonomy.promote("new hobby", evidence={
        "cluster_size": 3, "cluster_hrefs": [href], "description": "promoted before the field rename",
        "senso_citations": ["an old-style citation"], "reuse_check_cleared": True,
    })

    resp = client.get(f"/item/{href}")
    assert resp.status_code == 200
    assert b"an old-style citation" in resp.data


def test_item_detail_no_taxonomy_evidence_when_href_not_in_any_cluster(client, isolated_env):
    href = "https://example.com/a"
    write_plans(make_plan(plan_id="plan-x", interest="new hobby", items=[make_item(href=href)]))
    taxonomy.promote("new hobby", evidence={
        "cluster_size": 2, "cluster_hrefs": ["https://example.com/other-item"],
        "description": "unrelated cluster", "grounding_citations": [], "reuse_check_cleared": True,
    })

    resp = client.get(f"/item/{href}")
    assert resp.status_code == 200
    assert b"wasn't part of a cluster" in resp.data


def test_item_detail_shows_plan_grounding_and_recall(client, isolated_env):
    href = "https://example.com/a"
    plan = make_plan(plan_id="plan-x", items=[make_item(href=href)])
    plan["grounding"] = {"grounded": True, "citations": ["other saved post about this"], "source": "vectorai"}
    plan["memory"] = {"recalled": True, "memories": [
        {"status": "accepted", "score": 0.87, "subcategory": "hiking trails", "action": "go hiking"},
    ]}
    write_plans(plan)

    resp = client.get(f"/item/{href}")
    assert resp.status_code == 200
    assert b"other saved post about this" in resp.data
    assert b"0.87" in resp.data
    assert b"go hiking" in resp.data


def test_dashboard_item_row_links_to_item_detail(client, isolated_env):
    write_plans(make_plan(items=[make_item(href="https://example.com/a")]))
    resp = client.get("/")
    assert resp.status_code == 200
    assert b'/item/https://example.com/a' in resp.data


# ---------------------------------------------------------------------------
# GET /taxonomy
# ---------------------------------------------------------------------------

def test_taxonomy_view_empty_state(client, isolated_env):
    resp = client.get("/taxonomy")
    assert resp.status_code == 200
    assert b"No categories have been auto-promoted yet" in resp.data
    assert b"None." in resp.data  # seeded section, no history to attribute it to


def test_taxonomy_view_shows_seeded_categories(client, isolated_env):
    taxonomy.ensure_seeded(["cooking", "hiking"])
    resp = client.get("/taxonomy")
    assert resp.status_code == 200
    assert b"cooking" in resp.data
    assert b"hiking" in resp.data
    assert b"No categories have been auto-promoted yet" in resp.data


def test_taxonomy_view_shows_promotion_evidence_newest_first(client, isolated_env):
    taxonomy.promote("first category", evidence={
        "cluster_size": 2, "cluster_hrefs": ["https://example.com/a"],
        "description": "the first ever promoted category", "grounding_citations": ["cite one"],
        "reuse_check_cleared": True,
    })
    taxonomy.promote("second category", evidence={
        "cluster_size": 4, "cluster_hrefs": ["https://example.com/b"],
        "description": "a later promoted category", "grounding_citations": ["cite two"],
        "reuse_check_cleared": True,
    })

    resp = client.get("/taxonomy")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "second category" in body and "first category" in body
    # newest promotion (second category) should render before the older one
    assert body.index("second category") < body.index("first category")
    assert "a later promoted category" in body
    assert "cite two" in body
    assert "https://example.com/b" in body


def test_taxonomy_view_falls_back_to_legacy_senso_citations_field(client, isolated_env):
    """Real production data promoted before the Senso->VectorAI decoupling
    (ROADMAP.md §1) carries `senso_citations`, not `grounding_citations` —
    the view must still surface it, not silently render an empty list."""
    taxonomy.promote("legacy category", evidence={
        "cluster_size": 3, "cluster_hrefs": [], "description": "promoted before the field rename",
        "senso_citations": ["an old-style citation"], "reuse_check_cleared": True,
    })
    resp = client.get("/taxonomy")
    assert resp.status_code == 200
    assert b"an old-style citation" in resp.data


def test_dashboard_links_to_taxonomy_view(client, isolated_env):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"/taxonomy" in resp.data
