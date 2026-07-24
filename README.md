# Self-Evolving InstaGone Agent

Turns classified Instagram-post data into interest plans, publishes `cited.md`, and evolves
itself on real accept/reject feedback. Full architecture, honest real-vs-stub breakdown of
every component: **[RUNBOOK.md](RUNBOOK.md)**.

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

| Tool | Status |
|---|---|
| **Senso** (`apiv2.senso.ai`) | ✅ real — KB ingest + grounding |
| **VectorAI DB** (Actian, self-hosted) | ✅ real — episodic memory, taxonomy clustering |
| **Pioneer** (`api.pioneer.ai`) | ✅ real API call every retrain pass, blocked by account billing — the actual retraining outcome comes from a local reimplementation instead |
| **Guild** | local JSONL stub, no key |
| **Band** | not used — replaced with a local ACP-shaped orchestrator |
| **Replay.io** | credentialed, unused |
| **x402/CDP payments** | cosmetic banner only |

## Repo-internal pieces

`orchestrator.py` (local multi-agent Coordinator) · `loop.py` (pipeline) · `policy.py`/
`reclassify.py` (suppression) · `taxonomy.py`/`taxonomy_evolver.py` (auto-mints categories) ·
`reevaluator.py`/`tagger.py` (post-feedback reassignment) · `cancellation.py` · `store.py`
(snapshot/restore + locking).
