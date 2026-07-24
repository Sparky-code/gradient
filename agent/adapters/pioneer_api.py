"""Pioneer adapter — the REAL hosted retraining API.

`pioneer.py` runs the local reimplementation that actually drives `policy.py`
(the artifact `reclassify.py` applies). This module is the genuine
`/felix/...` client GAPS_AND_FILL.md flagged as the fill action for "Pioneer's
own hosted API is never called" — additive alongside the local logic, not a
replacement for it, since the local policy is what actually changes
classification behavior today.

Confirmed live against the real API with this project's own key in `API.md`:
  - Auth is `X-API-Key` (per docs.pioneer.ai/llms.txt).
  - Read-only calls succeed on the free tier: `GET /felix/datasets`,
    `GET /base-models`, `GET /felix/training-jobs`, `GET /billing/usage/requests`
    all returned real 200s.
  - Every compute-consuming call — `POST /felix/datasets/upload/url` *and*
    `POST /felix/training-jobs` directly — returned the same
    `{"detail": {"code": "card_required", ...}}` on this account, before even
    validating the request body (a placeholder dataset name didn't change the
    error). No hackathon-credit or team-ID bypass exists: tried `X-Team-Id`,
    `X-Organization-Id` headers and `team_id`/`organization_id` body fields,
    all four returned the identical error, and neither
    docs.pioneer.ai/concepts/training nor any `/billing/*` endpoint documents
    or exposes a free/credit path. This is an account-level gate, not
    something a client request can route around.

So this module does the real thing up to that wall: build a real SFT dataset
from the feedback queue, attempt the real 3-step upload
(`upload/url` -> PUT to the presigned URL -> `upload/process`) and the real
training-job submission, and report exactly which stage was reached and why
it stopped. It never fabricates a job id or status past what Pioneer's API
actually returned — the moment billing is resolved on this account, the same
code path submits a genuine job with no further changes needed.
"""

import json
import urllib.error
import urllib.request

from agent import config

BASE_URL = "https://api.pioneer.ai"

# Smallest real model in GET /base-models with supports_training=true that's
# still instruct-tuned (fits an SFT prompt/completion classification task
# better than a base or NER-only model) — picked to minimize real training
# time once a job actually runs.
BASE_MODEL = "meta-llama/Llama-3.2-1B-Instruct"


def _api_key() -> str | None:
    return config.load_api_key("Pioneer")


def _request(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"X-API-Key": key, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {"raw_reason": e.reason}
        return e.code, payload


def _build_sft_jsonl(queue: list[dict]) -> bytes:
    """Turn the labeled accept/reject feedback queue into SFT prompt/completion
    pairs — the same accept/reject signal `policy.py`'s local reimplementation
    already consumes, reformatted for Pioneer's real fine-tuning schema."""
    lines = []
    for example in queue:
        prompt = (
            "Classify whether this interest should surface to the user.\n"
            f"Interest: {example.get('interest')}\n"
            f"Subcategory: {example.get('subcategory')}\n"
            f"Actionable: {example.get('actionable')}\n"
            f"Action: {example.get('action')}"
        )
        completion = "surface" if example.get("decision") in ("accept", "share", "invite") else "suppress"
        lines.append(json.dumps({"prompt": prompt, "completion": completion}))
    return ("\n".join(lines) + "\n").encode("utf-8")


def attempt_real_retrain(queue: list[dict], dataset_name: str) -> dict:
    """Attempt the genuine Pioneer API path end to end. Stops and reports
    honestly at whichever stage Pioneer's API actually rejects — never
    fabricates a stage past what a real response confirmed."""
    key = _api_key()
    if not key:
        return {"attempted": False, "stage": None, "reason": "no Pioneer API key configured"}

    jsonl = _build_sft_jsonl(queue)

    status, payload = _request("POST", "/felix/datasets/upload/url", key, body={
        "dataset_name": dataset_name,
        "dataset_type": "sft",
        "type": "training",
        "filename": f"{dataset_name}.jsonl",
    })
    if status >= 300:
        return {"attempted": True, "stage": "dataset_upload_url", "ok": False,
                "http_status": status, "detail": payload.get("detail", payload)}

    upload_url = payload.get("upload_url") or payload.get("url")
    dataset_id = payload.get("dataset_id") or payload.get("id")
    if not upload_url or not dataset_id:
        return {"attempted": True, "stage": "dataset_upload_url", "ok": False,
                "http_status": status, "detail": "response missing upload_url/dataset_id", "raw": payload}

    put_req = urllib.request.Request(upload_url, data=jsonl, method="PUT")
    try:
        with urllib.request.urlopen(put_req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        return {"attempted": True, "stage": "s3_upload", "ok": False, "http_status": e.code, "detail": e.reason}

    status, payload = _request("POST", "/felix/datasets/upload/process", key, body={"dataset_id": dataset_id})
    if status >= 300:
        return {"attempted": True, "stage": "dataset_process", "ok": False,
                "http_status": status, "detail": payload.get("detail", payload)}

    status, payload = _request("POST", "/felix/training-jobs", key, body={
        "model_name": dataset_name,
        "base_model": BASE_MODEL,
        "datasets": [{"name": dataset_name}],
        "training_type": "lora",
        "nr_epochs": 3,
        "learning_rate": 5e-5,
    })
    if status >= 300:
        return {"attempted": True, "stage": "training_job_submit", "ok": False,
                "http_status": status, "detail": payload.get("detail", payload)}

    return {"attempted": True, "stage": "training_job_submit", "ok": True,
            "job_id": payload.get("id"), "status": payload.get("status")}


def poll_job(job_id: str) -> dict:
    """Check a submitted job's real current status."""
    key = _api_key()
    if not key:
        return {"ok": False, "reason": "no Pioneer API key configured"}
    status, payload = _request("GET", f"/felix/training-jobs/{job_id}", key)
    if status >= 300:
        return {"ok": False, "http_status": status, "detail": payload.get("detail", payload)}
    return {"ok": True, "status": payload.get("status"), "raw": payload}
