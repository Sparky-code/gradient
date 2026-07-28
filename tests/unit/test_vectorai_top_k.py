"""Unit tests for vectorai.top_k_anchors_many()/top_k_anchors() — the
multi-label variant of nearest_anchor_many() that scores a post against every
known category anchor instead of only the nearest one. No VectorAI DB/model
required: embed_batch() and the gRPC client are stubbed.
"""

from agent.adapters import vectorai


class _FakeHit:
    def __init__(self, name, score):
        self.payload = {"name": name}
        self.score = score


class _FakeSearch:
    def __init__(self, hits):
        self._hits = hits

    def search(self, collection, vector, limit, score_threshold):
        return [h for h in self._hits if h.score >= score_threshold][:limit]


class _FakeClient:
    def __init__(self, hits):
        self.points = _FakeSearch(hits)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_anchors(monkeypatch, hits):
    monkeypatch.setattr(vectorai, "embed_batch", lambda texts, kind: [[0.1] * vectorai.EMBEDDING_DIM for _ in texts])
    monkeypatch.setattr(vectorai, "_client", lambda: _FakeClient(hits))


def test_top_k_anchors_many_returns_multiple_scored_matches(monkeypatch):
    _stub_anchors(monkeypatch, [
        _FakeHit("travel and adventure", 0.91),
        _FakeHit("food and cooking", 0.7),
        _FakeHit("music and entertainment", 0.6),
    ])
    result = vectorai.top_k_anchors_many(["hiking trip photos"])
    assert result == [[
        {"name": "travel and adventure", "score": 0.91},
        {"name": "food and cooking", "score": 0.7},
        {"name": "music and entertainment", "score": 0.6},
    ]]


def test_top_k_anchors_many_respects_min_score(monkeypatch):
    _stub_anchors(monkeypatch, [
        _FakeHit("travel and adventure", 0.91),
        _FakeHit("unrelated topic", 0.3),
    ])
    result = vectorai.top_k_anchors_many(["hiking trip photos"], min_score=0.55)
    assert result == [[{"name": "travel and adventure", "score": 0.91}]]


def test_top_k_anchors_many_limits_to_k(monkeypatch):
    _stub_anchors(monkeypatch, [
        _FakeHit("a", 0.9), _FakeHit("b", 0.8), _FakeHit("c", 0.7),
    ])
    result = vectorai.top_k_anchors_many(["text"], k=2)
    assert result == [[{"name": "a", "score": 0.9}, {"name": "b", "score": 0.8}]]


def test_top_k_anchors_many_excludes_name(monkeypatch):
    _stub_anchors(monkeypatch, [
        _FakeHit("travel and adventure", 0.91),
        _FakeHit("food and cooking", 0.7),
    ])
    result = vectorai.top_k_anchors_many(["text"], exclude="travel and adventure")
    assert result == [[{"name": "food and cooking", "score": 0.7}]]


def test_top_k_anchors_many_degrades_when_embeddings_unavailable(monkeypatch):
    monkeypatch.setattr(vectorai, "embed_batch", lambda texts, kind: None)
    result = vectorai.top_k_anchors_many(["a", "b"])
    assert result == [[], []]


def test_top_k_anchors_many_empty_input_returns_empty_list():
    assert vectorai.top_k_anchors_many([]) == []


def test_top_k_anchors_single_text_convenience(monkeypatch):
    _stub_anchors(monkeypatch, [_FakeHit("travel and adventure", 0.91)])
    assert vectorai.top_k_anchors("hiking trip") == [{"name": "travel and adventure", "score": 0.91}]


def test_nearest_anchor_many_unaffected_by_refactor(monkeypatch):
    """Regression guard: the shared _anchor_search() helper must preserve
    nearest_anchor_many()'s exact existing single-dict-or-None contract."""
    _stub_anchors(monkeypatch, [
        _FakeHit("travel and adventure", 0.91),
        _FakeHit("food and cooking", 0.7),
    ])
    assert vectorai.nearest_anchor_many(["hiking trip"]) == [{"name": "travel and adventure", "score": 0.91}]


def test_nearest_anchor_many_exclude_unaffected_by_refactor(monkeypatch):
    _stub_anchors(monkeypatch, [
        _FakeHit("travel and adventure", 0.91),
        _FakeHit("food and cooking", 0.7),
    ])
    result = vectorai.nearest_anchor_many(["hiking trip"], exclude="travel and adventure")
    assert result == [{"name": "food and cooking", "score": 0.7}]


def test_nearest_anchor_many_no_match_returns_none(monkeypatch):
    _stub_anchors(monkeypatch, [])
    assert vectorai.nearest_anchor_many(["hiking trip"]) == [None]
