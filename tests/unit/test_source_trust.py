"""Unit tests for agent.source_trust — the usage-survival domain trust
registry (ROADMAP.md §3, docs/SOURCE_TRUST.md). Keyed on repeated calls to
record_pass() with agent.source_quality.score_sources()-shaped input.
"""

from agent import source_trust


def _scored(url, overall):
    return {"url": url, "quality": {"overall": overall}}


def test_unknown_domain_before_any_pass(isolated_env):
    assert source_trust.tier_for("https://example.com") == "unknown"
    assert source_trust.is_trusted("https://example.com") is False


def test_single_clean_pass_stays_unverified(isolated_env):
    source_trust.record_pass([_scored("https://example.com/a", 0.8)])
    assert source_trust.tier_for("https://example.com") == "unverified"


def test_promotes_after_promotion_streak_clean_passes(isolated_env):
    for _ in range(source_trust.PROMOTION_STREAK):
        source_trust.record_pass([_scored("https://example.com/a", 0.8)])
    assert source_trust.tier_for("https://example.com") == "trusted"
    assert source_trust.is_trusted("https://example.com") is True


def test_below_threshold_pass_does_not_promote(isolated_env):
    for _ in range(source_trust.PROMOTION_STREAK):
        source_trust.record_pass([_scored("https://example.com/a", source_trust.CLEAN_THRESHOLD - 0.01)])
    assert source_trust.tier_for("https://example.com") == "unverified"


def test_incident_resets_streak_before_promotion(isolated_env):
    source_trust.record_pass([_scored("https://example.com/a", 0.8)])
    source_trust.record_pass([_scored("https://example.com/a", 0.8)])
    source_trust.record_pass([_scored("https://example.com/a", 0.1)])  # incident, resets streak
    source_trust.record_pass([_scored("https://example.com/a", 0.8)])
    source_trust.record_pass([_scored("https://example.com/a", 0.8)])
    # only 2 clean in a row since the incident — not yet 3
    assert source_trust.tier_for("https://example.com") == "unverified"
    source_trust.record_pass([_scored("https://example.com/a", 0.8)])
    assert source_trust.tier_for("https://example.com") == "trusted"


def test_trusted_domain_demotes_immediately_on_a_single_incident(isolated_env):
    for _ in range(source_trust.PROMOTION_STREAK):
        source_trust.record_pass([_scored("https://example.com/a", 0.8)])
    assert source_trust.is_trusted("https://example.com") is True

    source_trust.record_pass([_scored("https://example.com/a", 0.1)])
    assert source_trust.is_trusted("https://example.com") is False
    assert source_trust.tier_for("https://example.com") == "unverified"


def test_promotion_and_demotion_recorded_in_history(isolated_env):
    for _ in range(source_trust.PROMOTION_STREAK):
        source_trust.record_pass([_scored("https://example.com/a", 0.8)])
    source_trust.record_pass([_scored("https://example.com/a", 0.1)])

    history = source_trust.load_current()["history"]
    events = [(h["domain"], h["event"]) for h in history]
    assert ("example.com", "promoted") in events
    assert ("example.com", "demoted") in events


def test_same_domain_twice_in_one_batch_counts_as_one_pass(isolated_env):
    source_trust.record_pass([
        _scored("https://example.com/a", 0.8),
        _scored("https://example.com/b", 0.8),
    ])
    record = source_trust.load_current()["domains"]["example.com"]
    assert record["total_passes"] == 1


def test_domains_tracked_independently(isolated_env):
    for _ in range(source_trust.PROMOTION_STREAK):
        source_trust.record_pass([_scored("https://good.com/a", 0.9)])
    source_trust.record_pass([_scored("https://bad.com/a", 0.1)])

    assert source_trust.tier_for("https://good.com") == "trusted"
    assert source_trust.tier_for("https://bad.com") == "unverified"


def test_explicit_rejected_feedback_overrides_high_automated_score(isolated_env):
    """A person's real accept/reject decision (once a learning-plan consumer
    exists) always wins over the automated composite — see record_pass()'s
    docstring."""
    source_trust.record_pass(
        [_scored("https://example.com/a", 0.95)],
        feedback={"https://example.com/a": "rejected"},
    )
    record = source_trust.load_current()["domains"]["example.com"]
    assert record["incidents"] == 1
    assert record["clean_streak"] == 0


def test_explicit_accepted_feedback_overrides_low_automated_score(isolated_env):
    source_trust.record_pass(
        [_scored("https://example.com/a", 0.1)],
        feedback={"https://example.com/a": "accepted"},
    )
    record = source_trust.load_current()["domains"]["example.com"]
    assert record["clean_streak"] == 1
    assert record["incidents"] == 0


def test_missing_url_is_skipped_without_crashing(isolated_env):
    result = source_trust.record_pass([{"url": None, "quality": {"overall": 0.9}}])
    assert result["domains"] == {}
