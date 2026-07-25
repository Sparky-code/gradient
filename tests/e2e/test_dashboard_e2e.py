"""Playwright, browser-driven coverage of webui.py's client-side behavior —
the dropzone, the fetch()-based per-item Accept/Reject buttons that update
badges without a full page reload, the alerts dropdown, and the confirm()-
gated reset button. Route-level/state-transition correctness is already
covered by tests/unit/test_routes.py against the Flask test client; these
tests exist to prove the real HTML/JS wired to those routes actually works
in a browser.
"""

from pathlib import Path

from agent import store

from tests.helpers import make_item, make_plan, write_plans


def test_empty_state(page, live_server):
    page.goto(live_server)
    assert page.get_by_text("No plans yet").is_visible()


def test_plan_and_items_render(page, live_server):
    write_plans(make_plan(
        plan_id="plan-x", interest="hiking",
        items=[make_item(href="https://example.com/a", subcategory="hiking trails", action="go hiking")],
    ))
    page.goto(live_server)
    assert page.get_by_text("hiking", exact=False).first.is_visible()
    assert page.get_by_text("hiking trails").is_visible()
    assert page.get_by_role("button", name="Accept").is_visible()
    assert page.get_by_role("button", name="Reject").is_visible()


def test_accept_item_updates_badge_without_reload(page, live_server):
    write_plans(make_plan(plan_id="plan-x", items=[make_item(href="https://example.com/a")]))
    page.goto(live_server)

    page.get_by_role("button", name="Accept").click()

    # Badges are CSS text-transform: uppercase — inner_text() reflects the
    # rendered (uppercased) text, not the underlying status string.
    item_badge = page.locator(".item-badge")
    assert item_badge.inner_text().strip().lower() == "accepted"
    plan_badge = page.locator(".plan-badge")
    assert plan_badge.inner_text().strip().lower() == "accepted"

    # Confirm it actually reached the server, not just client-side paint.
    def _persisted():
        plans = store.load_plans()
        return plans["plan-x"]["items"][0]["status"] == "accepted"

    page.wait_for_function(
        "document.querySelector('.item-badge').textContent.trim() === 'accepted'"
    )
    assert _wait_for(_persisted)


def test_mixed_decisions_reveal_submit_button(page, live_server):
    write_plans(make_plan(plan_id="plan-x", items=[
        make_item(href="https://example.com/a"),
        make_item(href="https://example.com/b"),
    ]))
    page.goto(live_server)

    accept_buttons = page.get_by_role("button", name="Accept")
    reject_buttons = page.get_by_role("button", name="Reject")
    accept_buttons.first.click()
    reject_buttons.nth(1).click()

    submit_form = page.locator("form.submit-plan-form")
    assert submit_form.first.is_visible()
    assert page.locator(".plan-badge").inner_text().strip().lower() == "mixed"


def test_submit_plan_clears_ready_flag(page, live_server):
    write_plans(make_plan(plan_id="plan-x", status="accepted", ready_to_submit=True,
                           items=[make_item(href="https://example.com/a", status="accepted")]))
    page.goto(live_server)

    # wait_for_url would be a no-op here (redirect target == current URL), so
    # wait for an actual navigation event instead.
    with page.expect_navigation():
        page.get_by_role("button", name="✓ Submit").click()

    def _submitted():
        return store.load_plans()["plan-x"]["ready_to_submit"] is False

    assert _wait_for(_submitted)


def test_upload_lands_file_in_drop_dir(page, live_server, isolated_env):
    page.goto(live_server)
    fixture_path = _write_temp_export(isolated_env["tmp_path"])
    # wait_for_url would be a no-op here (redirect target == current URL), so
    # wait for an actual navigation event instead.
    with page.expect_navigation():
        page.set_input_files("#export_file", str(fixture_path))
    assert (isolated_env["data_dir"] / "drop" / fixture_path.name).exists()


def test_run_cycle_button_completes_and_shows_alert(page, live_server):
    page.goto(live_server)
    # wait_for_url would be a no-op here (redirect target == current URL), so
    # wait for an actual navigation event instead.
    with page.expect_navigation():
        page.get_by_role("button", name="Run cycle now").click()

    # The faked run_once() is near-instant but still runs on a background
    # thread — give it a moment, then reload so the dropdown's server-rendered
    # content reflects the finished run rather than a mid-flight race.
    page.wait_for_timeout(300)
    page.reload()

    page.get_by_role("button", name="Alerts").click()
    assert page.get_by_text("Last run:", exact=False).is_visible()

    page.get_by_role("button", name="Clear").click()
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Alerts").click()
    assert page.get_by_text("No recent alerts").is_visible()


def test_reset_restores_last_snapshot(page, live_server):
    write_plans(make_plan(plan_id="plan-original"))
    store.snapshot("before change")
    write_plans(make_plan(plan_id="plan-after-change"))

    page.goto(live_server)
    page.on("dialog", lambda dialog: dialog.accept())
    # wait_for_url would be a no-op here (redirect target == current URL), so
    # wait for an actual navigation event instead.
    with page.expect_navigation():
        page.get_by_role("button", name="Reset to last good state").click()

    plans = store.load_plans()
    assert "plan-original" in plans
    assert "plan-after-change" not in plans


def test_view_cited_link_navigates_to_published_output(page, live_server):
    write_plans(make_plan(items=[make_item(subcategory="hiking trails")]))
    from agent import publisher
    publisher.render()

    page.goto(live_server)
    page.get_by_role("link", name="View raw cited.md").click()
    page.wait_for_url(live_server + "/cited")
    assert page.get_by_text("hiking trails").is_visible()


def _wait_for(condition, timeout=5.0, interval=0.05):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def _write_temp_export(tmp_path: Path) -> Path:
    p = tmp_path / "export.json"
    p.write_text('{"posts": []}')
    return p
