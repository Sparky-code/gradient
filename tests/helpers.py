"""Plain (non-fixture) helpers shared across the unit and e2e suites — kept
out of conftest.py so they can be imported normally (`from tests.helpers
import ...`) instead of only being available as pytest fixtures.
"""

import time

from agent import store


def make_item(href="https://www.instagram.com/reel/AAA111/", subcategory="test subcategory",
              actionable="wishlist_place", action="do the test thing", status="pending",
              key_facts=None, tags=None):
    item = {
        "href": href,
        "subcategory": subcategory,
        "actionable": actionable,
        "action": action,
        "key_facts": key_facts if key_facts is not None else [],
        "status": status,
    }
    if tags is not None:
        item["tags"] = tags
    return item


def make_plan(plan_id="plan-test-interest", interest="test interest", status="pending",
              items=None, generated_at="2026-01-01T00:00:00+00:00", ready_to_submit=False):
    return {
        "plan_id": plan_id,
        "interest": interest,
        "status": status,
        "generated_at": generated_at,
        "items": items if items is not None else [make_item()],
        "ready_to_submit": ready_to_submit,
    }


def write_plans(*plans):
    """Persist plans via store.save_plans() — call only after isolated_env is active."""
    store.save_plans({p["plan_id"]: p for p in plans})


def wait_until(condition, timeout=5.0, interval=0.02):
    """Poll a zero-arg callable until it returns truthy or the timeout elapses.
    Background threads (run/submit) finish asynchronously from the route's
    perspective, so route tests need this instead of asserting immediately."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()
