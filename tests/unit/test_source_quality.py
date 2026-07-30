"""Unit tests for agent.source_quality — the composite that scores external
search results before they'd ground a learning plan (ROADMAP.md §3). The
LLM-judge worker itself isn't exercised here (heavy: local model subprocess);
_judge_via_worker's degrade-gracefully path and score_sources()'s combination
logic are, matching how tagger.py/reclassify.py are tested elsewhere.
"""

from agent import source_quality


def _result(url="https://example.com/a", title="A guide", excerpt="", raw_content=None, published_date=None):
    return {"url": url, "title": title, "excerpt": excerpt, "raw_content": raw_content, "published_date": published_date}


# ---------------------------------------------------------------------------
# _structural_score
# ---------------------------------------------------------------------------

def test_structural_score_https_only():
    assert source_quality._structural_score(_result(url="https://example.com")) == 0.4


def test_structural_score_http_gets_no_https_credit():
    assert source_quality._structural_score(_result(url="http://example.com")) == 0.0


def test_structural_score_byline_marker_in_excerpt():
    result = _result(excerpt="Written by Jane Doe, a professional chef.")
    assert source_quality._structural_score(result) == 0.4 + 0.3  # https + byline


def test_structural_score_about_page_marker():
    result = _result(raw_content="Some article text. Contact us at hello@example.com.")
    assert source_quality._structural_score(result) == 0.4 + 0.15


def test_structural_score_published_date_present():
    result = _result(published_date="2026-01-01")
    assert source_quality._structural_score(result) == 0.4 + 0.15


def test_structural_score_caps_at_one():
    result = _result(
        excerpt="Written by Jane Doe. About the author: a chef.",
        published_date="2026-01-01",
    )
    assert source_quality._structural_score(result) == 1.0


def test_structural_score_raw_content_preferred_over_excerpt():
    result = _result(excerpt="nothing relevant here", raw_content="Written by Jane Doe")
    assert source_quality._structural_score(result) == 0.4 + 0.3


# ---------------------------------------------------------------------------
# _domain / _keywords
# ---------------------------------------------------------------------------

def test_domain_strips_scheme_and_www():
    assert source_quality.domain_of("https://www.Example.com/some/path") == "example.com"


def test_domain_empty_for_missing_url():
    assert source_quality.domain_of("") == ""


def test_keywords_filters_stopwords_and_short_words():
    kws = source_quality._keywords("How to learn Python for data science")
    assert "python" in kws
    assert "data" in kws
    assert "science" in kws
    assert "how" not in kws  # stopword
    assert "to" not in kws  # stopword, also too short


# ---------------------------------------------------------------------------
# _corroboration_flags
# ---------------------------------------------------------------------------

def test_corroboration_true_for_overlapping_different_domain_results():
    results = [
        _result(url="https://a.com", excerpt="python virtual environments and dependency management"),
        _result(url="https://b.com", excerpt="managing python dependencies with virtual environments"),
    ]
    assert source_quality._corroboration_flags(results) == [True, True]


def test_corroboration_false_for_unrelated_results():
    results = [
        _result(url="https://a.com", excerpt="python virtual environments and dependency management"),
        _result(url="https://b.com", excerpt="sourdough bread baking techniques and hydration ratios"),
    ]
    assert source_quality._corroboration_flags(results) == [False, False]


def test_corroboration_same_domain_does_not_count():
    results = [
        _result(url="https://a.com/1", excerpt="python virtual environments and dependency management"),
        _result(url="https://a.com/2", excerpt="managing python dependencies with virtual environments"),
    ]
    assert source_quality._corroboration_flags(results) == [False, False]


def test_corroboration_single_result_is_never_corroborated():
    results = [_result(excerpt="python virtual environments")]
    assert source_quality._corroboration_flags(results) == [False]


# ---------------------------------------------------------------------------
# _overall
# ---------------------------------------------------------------------------

def test_overall_structural_only_when_no_llm_score():
    assert source_quality._overall(0.6, None, corroborated=False) == 0.6


def test_overall_averages_structural_and_llm_score():
    assert source_quality._overall(0.6, 0.8, corroborated=False) == 0.7


def test_overall_corroboration_bump_applied_and_capped():
    assert source_quality._overall(0.9, 0.9, corroborated=True) == 1.0  # 0.9 + 0.15 capped
    assert source_quality._overall(0.4, 0.4, corroborated=True) == 0.55


# ---------------------------------------------------------------------------
# _judge_via_worker / score_sources — degrade-gracefully + combination logic
# ---------------------------------------------------------------------------

def test_judge_via_worker_degrades_when_venv_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(source_quality, "VENV_PYTHON", tmp_path / "no-such-python")
    results = [_result(), _result(url="https://b.com")]
    assert source_quality._judge_via_worker(results) == [
        {"score": None, "reason": None}, {"score": None, "reason": None},
    ]


def test_score_sources_empty_input():
    assert source_quality.score_sources([]) == []


def test_score_sources_combines_all_signals(monkeypatch):
    monkeypatch.setattr(
        source_quality, "_judge_via_worker",
        lambda results: [{"score": 0.8, "reason": "reads as credible"} for _ in results],
    )
    results = [
        _result(url="https://a.com", excerpt="python virtual environments and dependency management"),
        _result(url="https://b.com", excerpt="managing python dependencies with virtual environments"),
    ]
    enriched = source_quality.score_sources(results)

    assert len(enriched) == 2
    assert enriched[0]["url"] == "https://a.com"  # original fields preserved
    quality = enriched[0]["quality"]
    assert quality["llm_judge_score"] == 0.8
    assert quality["llm_judge_reason"] == "reads as credible"
    assert quality["corroborated"] is True
    assert quality["structural_score"] == 0.4  # https only, no byline/date in these excerpts
    assert quality["overall"] == round(min(1.0, (0.4 + 0.8) / 2 + 0.15), 2)


def test_score_sources_degrades_when_worker_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(source_quality, "VENV_PYTHON", tmp_path / "no-such-python")
    results = [_result()]
    enriched = source_quality.score_sources(results)
    assert enriched[0]["quality"]["llm_judge_score"] is None
    assert enriched[0]["quality"]["overall"] == enriched[0]["quality"]["structural_score"]
