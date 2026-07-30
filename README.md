# Gradient

Turns already-classified Instagram-post data into interest plans, publishes `cited.md`, and
evolves itself on real accept/reject feedback. Gradient's own scope starts after a raw export
has been enriched (see `agent/ingest.py`) — planning, grounding, taxonomy evolution, and the
feedback loop, not downloading/transcribing/classifying raw Instagram media itself. Full
architecture, honest real-vs-stub breakdown of every component: **[RUNBOOK.md](RUNBOOK.md)**.
Where this goes next: **[ROADMAP.md](ROADMAP.md)**.

## Quickstart — web dashboard

```bash
docker compose up -d              # starts VectorAI DB (skip if already running: docker compose ps)
./venv/bin/python webui.py         # localhost-only, http://127.0.0.1:5000
```

That's it if `venv/` already exists. From scratch:

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt mlx-lm torch transformers
```

Open `
` — upload a drop file, trigger a pass, give per-item feedback, and
read `cited.md`, all from the browser. Full walkthrough: **[docs/DEMO.md](docs/DEMO.md)** §6.

## Stack

- **Python 3**, one shared `venv/`
- **Flask** — `webui.py`, localhost-only dashboard
- **Docker** — self-hosted VectorAI DB container

## Local models

| Model | Via | Used for |
|---|---|---|
| `mlx-community/Qwen3-30B-A3B-4bit` | `mlx_lm` | policy reclassification, taxonomy category naming, item tagging |
| `nomic-ai/nomic-embed-text-v1.5` | `torch`/`transformers` | VectorAI DB embeddings (recall + clustering) |

Both subprocess into `venv/` in isolation, batched per pass, cooperatively cancellable.

## Sponsor tools

Local-first: a small number of genuinely real integrations, not a stub for every tool named in
the brief.

| Tool | Status |
|---|---|
| **VectorAI DB** (Actian, self-hosted) | ✅ real — episodic memory, taxonomy clustering, and now grounding too (see below) |
| **Pioneer** (`api.pioneer.ai`) | ✅ real API call every retrain pass, blocked by account billing — the actual retraining outcome comes from a local reimplementation instead |

Not integrated, on purpose: **Senso** (decoupled — grounding used to search a hosted KB
containing nothing this project's own corpus didn't already push into it; `vectorai.ground_locally()`
now grounds an interest in other posts the user actually saved via VectorAI DB's own local search,
same collection `recall_similar_many()` already used — see `ROADMAP.md` §1), **Guild**
(governance/audit-logging is `agent/session_log.py`, a genuine first-party local log — not a stub
waiting for a hosted API), **Band** (replaced with a local ACP-shaped orchestrator), **Replay.io**
(dropped outright — a live credential sat unused with zero adapter code; removed rather than kept
as a someday-gap), **x402/CDP payments** (monetization dropped outright as out of scope — no
paywall, real or cosmetic).

## Repo-internal pieces

`orchestrator.py` (local multi-agent Coordinator) · `loop.py` (pipeline) · `policy.py`/
`reclassify.py` (suppression) · `taxonomy.py`/`taxonomy_evolver.py` (auto-mints categories) ·
`reevaluator.py`/`tagger.py` (post-feedback reassignment) · `cancellation.py` · `store.py`
(snapshot/restore + locking).
