#!/usr/bin/env python3
"""Localhost-only dashboard for the self-evolving agent.

Run with `./venv/bin/python webui.py` — binds to 127.0.0.1 only, never the
network. Three things a person needs to do by hand (drop new data, trigger a
pass, give feedback) plus a read-only view of the published output, all from
a browser instead of the CLI (see DEMO.md for the CLI-only version).

Routes:
  GET  /              dashboard — plans, statuses, session log tail, policy version.
                      Auto-refreshes every 5s while a run or plan-submit is in
                      progress (plain meta-refresh, not a separate status
                      endpoint/JS poll).
  POST /upload         save an uploaded export JSON into data/drop/ — disabled
                      (server-rejected) while a run or submit is in progress
  POST /run             trigger main.py's run_once() in a background thread
  POST /feedback         accept/reject/share/invite on one item in a plan
  POST /submit-plan      once a plan is fully resolved (all items decided),
                      run the tag/reassign/new-category pass in the
                      background — feedback.submit_plan()
  POST /cancel           request cancellation of whatever's running (a full
                      run or a plan submission) — agent/cancellation.py
  POST /reset            restore plans/cited.md/policy/taxonomy from the most
                      recent automatic snapshot — store.snapshot()/
                      restore_snapshot(), taken before every run cycle and
                      plan submission
  POST /alerts/clear      dismiss the alerts dropdown's contents (last
                      run/submit result or error) — otherwise these persist
                      forever across page loads
  GET  /cited            raw cited.md
"""

import json
import threading

from flask import Flask, redirect, render_template_string, request, url_for

from agent import cancellation, config, feedback, loop, policy, store

app = Flask(__name__)

_run_lock = threading.Lock()
_run_state = {"running": False, "last_result": None, "last_error": None}

_submit_lock = threading.Lock()
_submit_state = {"running": False, "plan_id": None, "last_result": None, "last_error": None}


def _run_in_background() -> None:
    try:
        result = loop.run_once()
        _run_state["last_result"] = result
        _run_state["last_error"] = None
    except Exception as e:  # noqa: BLE001 - surface any failure to the dashboard, don't crash the thread silently
        _run_state["last_error"] = str(e)
    finally:
        _run_state["running"] = False
        _run_lock.release()


def _submit_in_background(plan_id: str) -> None:
    try:
        result = feedback.submit_plan(plan_id)
        _submit_state["last_result"] = result
        _submit_state["last_error"] = None
    except Exception as e:  # noqa: BLE001 - same reasoning as _run_in_background above
        _submit_state["last_error"] = str(e)
    finally:
        _submit_state["running"] = False
        _submit_lock.release()


def _session_log_tail(n: int = 15) -> list[dict]:
    """Each entry gets an `extra` field (everything but logged_at/event) precomputed
    here — Jinja2 can't evaluate a Python dict comprehension inside {{ }}."""
    if not config.SESSION_LOG_FILE.exists():
        return []
    lines = [l for l in config.SESSION_LOG_FILE.read_text().splitlines() if l.strip()]
    entries = [json.loads(l) for l in lines[-n:]][::-1]
    for e in entries:
        e["extra"] = {k: v for k, v in e.items() if k not in ("logged_at", "event", "extra")}
    return entries


PAGE = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gradient — Dashboard</title>
{% if run_state.running or submit_state.running %}<meta http-equiv="refresh" content="5">{% endif %}
<style>
  :root {
    --bg: #f5f6fa;
    --surface: #ffffff;
    --border: #e3e5ea;
    --text: #1c1e21;
    --text-muted: #6b7280;
    --accent: #1b4332;
    --accent-hover: #163a2a;
    --pending: #d4a017;
    --accepted: #2f9e6e;
    --rejected: #d64545;
    --linked: #3b82f6;
    --mixed: #8b5cf6;
    --radius: 10px;
    --shadow: 0 1px 2px rgba(16,24,40,.04), 0 1px 3px rgba(16,24,40,.06);
    --tooltip-bg: #1c1e21;
    --tooltip-text: #f5f6fa;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14161a;
      --surface: #1d2025;
      --border: #2b2f36;
      --text: #e7e9ec;
      --text-muted: #9aa1ab;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 1px 3px rgba(0,0,0,.5);
      --tooltip-bg: #2b2f36;
      --tooltip-text: #e7e9ec;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg); color: var(--text);
    max-width: 960px; margin: 0 auto; padding: 1.5rem 1.25rem 4rem;
    line-height: 1.5;
  }
  a { color: var(--linked); }
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; margin-bottom: 1.5rem;
  }
  h1 { font-size: 1.4rem; margin: 0; }
  .subtitle { margin: .15rem 0 0; color: var(--text-muted); font-size: .85rem; }
  .section-title {
    font-size: 1.05rem; margin: 2rem 0 .75rem; display: flex; align-items: center; gap: .5rem;
  }
  .count {
    font-size: .75rem; font-weight: normal; color: var(--text-muted);
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 999px; padding: .1rem .55rem;
  }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow); padding: 1rem 1.25rem; margin-bottom: 1rem;
  }
  .card h2 { margin-top: 0; font-size: 1rem; }
  .stat-row { display: flex; gap: 2.5rem; align-items: baseline; flex-wrap: wrap; }
  .stat-label { display: block; font-size: .75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: .04em; }
  .stat-value { display: block; font-size: 1.3rem; font-weight: 600; }
  .meta { color: var(--text-muted); font-size: .85rem; }
  .meta.full { flex-basis: 100%; margin: .25rem 0 0; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }

  .banner {
    background: #fff6e0; border: 1px solid var(--pending); padding: .7rem 1rem;
    border-radius: var(--radius); margin-bottom: 1rem;
  }
  .banner.error { background: #ffecec; border-color: var(--rejected); }
  .banner.success { background: #e9f7ef; border-color: var(--accepted); }
  @media (prefers-color-scheme: dark) {
    .banner { background: #332c13; }
    .banner.error { background: #3a1e1e; }
    .banner.success { background: #16301f; }
  }

  .alerts-menu { position: relative; }
  .alerts-btn {
    position: relative; width: 34px; height: 34px; border-radius: 50%;
    border: 1px solid var(--border); background: var(--surface); color: var(--text);
    font-size: 1rem; cursor: pointer; display: flex; align-items: center; justify-content: center;
  }
  .alerts-btn:hover { border-color: var(--linked); }
  .alerts-dot {
    position: absolute; top: 3px; right: 3px; width: 9px; height: 9px;
    border-radius: 50%; background: var(--rejected); border: 2px solid var(--surface);
  }
  .alerts-dropdown {
    display: none; position: absolute; top: calc(100% + 8px); right: 0; z-index: 20;
    width: min(440px, 90vw); max-height: 70vh; overflow-y: auto;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); box-shadow: var(--shadow); padding: .65rem;
  }
  .alerts-dropdown.open { display: block; }
  .alert-item {
    padding: .6rem .7rem; border-radius: 8px; margin-bottom: .5rem; font-size: .82rem;
    background: rgba(127,127,127,.08); word-break: break-word; line-height: 1.45;
  }
  .alert-item:last-of-type { margin-bottom: 0; }
  .alert-item.error { background: #ffecec; color: #7a1f1f; }
  .alert-item.success { background: #e9f7ef; color: #14432a; }
  @media (prefers-color-scheme: dark) {
    .alert-item.error { background: #3a1e1e; color: #ffb4b4; }
    .alert-item.success { background: #16301f; color: #b7e6c9; }
  }
  .alerts-empty { font-size: .85rem; color: var(--text-muted); margin: 0; padding: .4rem .5rem; }
  .alerts-clear-form { margin: .5rem 0 0; text-align: right; }
  .alerts-clear-btn {
    font-size: .78rem; border: 1px solid var(--border); background: var(--bg); color: var(--text-muted);
    border-radius: 6px; padding: .3rem .6rem; cursor: pointer;
  }
  .alerts-clear-btn:hover { color: var(--text); border-color: var(--text-muted); }

  .plan-card {
    background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--border);
    border-radius: var(--radius); box-shadow: var(--shadow);
    padding: .85rem 1.1rem; margin: .6rem 0; transition: box-shadow .15s ease;
  }
  .plan-card:hover { box-shadow: 0 2px 6px rgba(16,24,40,.1); }
  .plan-card.pending { border-left-color: var(--pending); }
  .plan-card.accepted { border-left-color: var(--accepted); }
  .plan-card.rejected { border-left-color: var(--rejected); }
  .plan-card.shared, .plan-card.invited { border-left-color: var(--linked); }
  .plan-card.mixed { border-left-color: var(--mixed); }
  .plan-head { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
  .badge {
    font-size: .7rem; font-weight: 600; text-transform: uppercase; letter-spacing: .03em;
    padding: .15rem .5rem; border-radius: 999px; color: white; background: var(--text-muted);
  }
  .badge.pending { background: var(--pending); }
  .badge.accepted { background: var(--accepted); }
  .badge.rejected { background: var(--rejected); }
  .badge.shared, .badge.invited { background: var(--linked); }
  .badge.mixed { background: var(--mixed); }
  .badge.small { font-size: .62rem; padding: .05rem .4rem; margin-right: .4rem; }
  .empty { color: var(--text-muted); font-style: italic; }

  [data-tooltip] { position: relative; }
  [data-tooltip]:hover::after, [data-tooltip]:focus-visible::after {
    content: attr(data-tooltip);
    position: absolute; top: calc(100% + 8px); left: 50%; transform: translateX(-50%);
    width: max-content; max-width: 260px; background: var(--tooltip-bg); color: var(--tooltip-text);
    font-size: .72rem; font-weight: 400; line-height: 1.4; text-align: left; white-space: normal;
    padding: .5rem .65rem; border-radius: 8px; box-shadow: var(--shadow); z-index: 20;
    pointer-events: none;
  }
  .info-icon {
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.05rem; height: 1.05rem; border-radius: 50%; flex: 0 0 auto;
    background: var(--border); color: var(--text-muted);
    font-size: .68rem; font-weight: 700; cursor: help;
  }
  .info-icon:hover, .info-icon:focus-visible { background: var(--linked); color: white; outline: none; }
  .stat-row-header { display: flex; align-items: center; gap: .4rem; flex-basis: 100%; margin-bottom: .25rem; }
  .stat-row-header h2 { margin: 0; }

  details.items-toggle { margin: .5rem 0 .1rem; }
  details.items-toggle summary {
    cursor: pointer; font-size: .85rem; color: var(--text-muted); list-style: none;
    display: flex; align-items: center; gap: .4rem; user-select: none;
  }
  details.items-toggle summary::-webkit-details-marker { display: none; }
  details.items-toggle summary::before {
    content: '▸'; display: inline-block; font-size: .7rem; transition: transform .15s ease;
  }
  details.items-toggle[open] summary::before { transform: rotate(90deg); }
  details.items-toggle summary:hover { color: var(--text); }

  ul.items { list-style: none; margin: .5rem 0 0; padding: 0; display: flex; flex-direction: column; gap: .4rem; }
  .item-row {
    display: flex; align-items: center; justify-content: space-between; gap: .75rem;
    border: 1px solid var(--border); border-left: 3px solid var(--border);
    border-radius: 8px; padding: .45rem .65rem; background: rgba(127,127,127,.03); flex-wrap: wrap;
  }
  .item-row.pending { border-left-color: var(--pending); }
  .item-row.accepted { border-left-color: var(--accepted); }
  .item-row.rejected { border-left-color: var(--rejected); opacity: .65; }
  .item-text { font-size: .85rem; flex: 1 1 320px; }
  .item-feedback { margin: 0; flex: 0 0 auto; }

  .submit-plan-form { margin-top: .6rem; }
  button.submit-plan {
    background: var(--accent); color: white; border: none; font-weight: 600;
    padding: .4rem .85rem; border-radius: 6px; font-size: .85rem;
  }
  button.submit-plan:hover:not(:disabled) { background: var(--accent-hover); }
  button.submit-plan:disabled { opacity: .6; cursor: default; }

  code {
    background: rgba(127,127,127,.12); padding: .1rem .35rem; border-radius: 4px;
    font-size: .85em;
  }

  form.feedback { display: flex; gap: .4rem; flex-wrap: wrap; margin-top: .5rem; }
  button {
    cursor: pointer; border: 1px solid var(--border); background: var(--surface); color: var(--text);
    border-radius: 6px; padding: .35rem .7rem; font-size: .85rem;
    transition: background .12s ease, border-color .12s ease, transform .05s ease;
  }
  button:hover { border-color: var(--text-muted); }
  button:active { transform: translateY(1px); }
  button[value="accept"]:hover { border-color: var(--accepted); color: var(--accepted); }
  button[value="reject"]:hover { border-color: var(--rejected); color: var(--rejected); }
  button[value="share"]:hover, button[value="invite"]:hover { border-color: var(--linked); color: var(--linked); }
  button.run {
    background: var(--accent); color: white; font-weight: 600; padding: .6rem 1.2rem;
    border: none; border-radius: 8px; font-size: .9rem; box-shadow: var(--shadow);
  }
  button.run:hover:not(:disabled) { background: var(--accent-hover); }
  button.run:disabled { opacity: .6; cursor: default; }

  .topbar-actions { display: flex; align-items: center; gap: .6rem; }
  .reset-form { margin: 0; }
  button.reset-btn {
    background: var(--surface); color: var(--text-muted); border: 1px solid var(--border);
    font-weight: 600; padding: .55rem 1rem; border-radius: 8px; font-size: .85rem;
  }
  button.reset-btn:hover { border-color: var(--pending); color: var(--pending); }

  .link-card {
    display: inline-block; background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: .6rem 1rem; text-decoration: none; box-shadow: var(--shadow);
  }

  .dropzone {
    position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: .25rem; text-align: center; cursor: pointer; margin: 0;
    border: 2px dashed var(--border); border-radius: var(--radius);
    padding: 1.75rem 1rem; color: var(--text-muted); font-size: .85rem;
    transition: border-color .15s ease, background .15s ease, color .15s ease;
  }
  .dropzone:hover { border-color: var(--linked); color: var(--text); }
  .dropzone.dragover { border-color: var(--linked); background: rgba(59,130,246,.08); color: var(--text); }
  .dropzone input[type="file"] { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
  .dropzone-text { font-weight: 600; }
  .dropzone-hint { font-size: .75rem; }
  .dropzone-status { font-size: .75rem; color: var(--linked); min-height: 1.1em; }

  .processing {
    display: flex; align-items: center; gap: .85rem;
    border: 2px dashed var(--linked); border-radius: var(--radius);
    padding: 1.5rem 1.25rem; background: rgba(59,130,246,.06);
  }
  .processing-bar {
    flex: 1 1 auto; height: 8px; border-radius: 999px; overflow: hidden;
    background: rgba(59,130,246,.15);
  }
  .processing-bar-fill {
    height: 100%; width: 40%; border-radius: 999px;
    background: var(--linked);
    animation: processing-sweep 1.4s ease-in-out infinite;
  }
  @keyframes processing-sweep {
    0%   { transform: translateX(-100%); }
    100% { transform: translateX(250%); }
  }
  .processing-text { font-size: .85rem; font-weight: 600; white-space: nowrap; }
  .cancel-form { margin: 0; flex: 0 0 auto; }
  .cancel-btn {
    width: 28px; height: 28px; border-radius: 50%; padding: 0; line-height: 1;
    border: 1px solid var(--border); background: var(--surface); color: var(--text-muted);
    font-size: 1.1rem; cursor: pointer;
  }
  .cancel-btn:hover { border-color: var(--rejected); color: var(--rejected); }
  .processing-hint { margin-top: .6rem; }

  .table-wrap { padding: 0; overflow: hidden; }
  table.log { width: 100%; border-collapse: collapse; font-size: .8rem; }
  table.log td { padding: .5rem .75rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  table.log tr:last-child td { border-bottom: none; }
  table.log tr:hover td { background: rgba(127,127,127,.06); }
  .event {
    font-weight: 600; background: rgba(59,130,246,.12); color: var(--linked);
    padding: .1rem .45rem; border-radius: 4px; font-size: .78rem;
  }
</style>
</head>
<body>

<header class="topbar">
  <div>
    <h1>Gradient</h1>
    <p class="subtitle">Local dashboard — plans, policy, activity</p>
  </div>
  <div class="topbar-actions">
    {% if latest_snapshot and not (run_state.running or submit_state.running) %}
    <form method="post" action="{{ url_for('reset_state') }}" class="reset-form"
      onsubmit="return confirm('Reset all state back to the snapshot taken {{ latest_snapshot.created_at }} ({{ latest_snapshot.label }})? This discards anything that happened after that point.');">
      <button type="submit" class="reset-btn"
        data-tooltip="Restores plans.json, cited.md, policy, and taxonomy state from the most recent automatic snapshot — taken right before the last run cycle or plan submission. Latest: {{ latest_snapshot.label }} ({{ latest_snapshot.created_at }}).">
        Reset to last good state
      </button>
    </form>
    {% endif %}
    <div class="alerts-menu">
      <button type="button" class="alerts-btn" id="alerts-toggle" aria-label="Alerts"
        data-tooltip="Recent run/submission results and errors.">
        &#128276;
        {% if run_state.last_error or run_state.last_result or submit_state.last_error or submit_state.last_result %}
        <span class="alerts-dot"></span>
        {% endif %}
      </button>
      <div class="alerts-dropdown" id="alerts-dropdown">
        {% if run_state.last_error %}
          <div class="alert-item error"><strong>Last run failed:</strong> {{ run_state.last_error }}</div>
        {% elif run_state.last_result %}
          {% if run_state.last_result.cancelled %}
          <div class="alert-item">Last run was cancelled partway through.</div>
          {% else %}
          <div class="alert-item success">Last run: {{ run_state.last_result }}</div>
          {% endif %}
        {% endif %}
        {% if submit_state.last_error %}
          <div class="alert-item error"><strong>Last plan submission failed:</strong> {{ submit_state.last_error }}</div>
        {% elif submit_state.last_result and submit_state.last_result.cancelled %}
          <div class="alert-item">Plan submission was cancelled — nothing was changed, submit again when ready.</div>
        {% elif submit_state.last_result %}
          <div class="alert-item success">Submitted <code>{{ submit_state.last_result.plan_id }}</code>:
            tagged {{ submit_state.last_result.tagged }},
            reassigned {{ submit_state.last_result.reassigned|length }}
            {%- if submit_state.last_result.taxonomy_promoted %}, promoted new category
            "{{ submit_state.last_result.taxonomy_promoted }}"{% endif %}
            {%- if submit_state.last_result.still_unassigned %}, {{ submit_state.last_result.still_unassigned }}
            still unassigned (filed under "other"){% endif %}.</div>
        {% endif %}
        {% if not (run_state.last_error or run_state.last_result or submit_state.last_error or submit_state.last_result) %}
          <p class="alerts-empty">No recent alerts.</p>
        {% else %}
          <form method="post" action="{{ url_for('clear_alerts') }}" class="alerts-clear-form">
            <button type="submit" class="alerts-clear-btn">Clear</button>
          </form>
        {% endif %}
      </div>
    </div>
    <form method="post" action="{{ url_for('trigger_run') }}">
      <button class="run" type="submit" {{ 'disabled' if run_state.running else '' }}
        data-tooltip="Runs one full pass: ingests any new files in data/drop/, reclassifies posts against the current policy, groups them into plans, grounds/recalls context, publishes cited.md, then checks whether 5+ feedback examples have queued up to auto-promote a new policy.">
        {{ 'Running…' if run_state.running else 'Run cycle now' }}
      </button>
    </form>
  </div>
</header>

<main>

<section class="card">
  <h2>Upload a new export</h2>
  {% if run_state.running or submit_state.running %}
    <div class="processing">
      <div class="processing-bar"><div class="processing-bar-fill"></div></div>
      <span class="processing-text">
        {% if submit_state.running %}
          Reassigning <code>{{ submit_state.plan_id }}</code>…
        {% else %}
          Running a pass…
        {% endif %}
      </span>
      <form method="post" action="{{ url_for('cancel') }}" class="cancel-form">
        <button type="submit" class="cancel-btn" title="Cancel" aria-label="Cancel">&times;</button>
      </form>
    </div>
    <p class="meta full processing-hint">Local-model steps (reclassify, tag, embed) can take a
      while on the first run of a session (model load) — see BENCHMARKS.md. Upload is disabled
      until this finishes or is cancelled.</p>
  {% else %}
  <form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data" id="upload-form">
    <label class="dropzone" id="dropzone" for="export_file">
      <input type="file" name="export_file" id="export_file" accept=".json">
      <span class="dropzone-text">Drag a JSON export here, or click to choose a file</span>
      <span class="dropzone-hint">...</span>
      <span class="dropzone-status" id="dropzone-status"></span>
    </label>
  </form>
  {% endif %}
</section>

<section class="card stat-row">
  <div class="stat-row-header">
    <h2>Policy</h2>
    <span class="info-icon" tabindex="0" aria-label="What is a policy?"
      data-tooltip="Every Accept/Reject you give is queued as a training example. Once 5 are queued, the next run cycle bundles them into a new policy version — a few-shot exemplar set built from your real decisions — and auto-promotes it, no approval step. That policy then shapes how future posts get classified. This only updates at the end of a run cycle, not the instant you click Accept/Reject.">i</span>
  </div>
  <div>
    <span class="stat-label">Policy version</span>
    <span class="stat-value">{{ current_policy.version }}</span>
  </div>
  <div>
    <span class="stat-label">Active exemplars</span>
    <span class="stat-value">{{ current_policy.exemplars|length }}</span>
  </div>
  <p class="meta full">Every 5 feedback examples auto-promotes a new policy — checked at the end of each run cycle, not immediately on Accept/Reject. See RUNBOOK.md §9.</p>
</section>

<h2 class="section-title">Plans <span class="count">{{ plans|length }}</span></h2>
{% for plan in plans %}
  <div class="plan-card {{ plan.status }}">
    <div class="plan-head">
      <b>{{ plan.interest }}</b>
      <span class="badge plan-badge {{ plan.status }}">{{ plan.status }}</span>
    </div>
    <div class="meta">
      <code>{{ plan.plan_id }}</code>
      · {{ plan['items']|length }} item(s)
      {% if plan.grounding and plan.grounding.grounded %}· grounded{% endif %}
      {% if plan.memory and plan.memory.recalled %}· {{ plan.memory.memories|length }} VectorAI DB recall(s){% endif %}
    </div>
    <details class="items-toggle" {{ 'open' if plan['items']|length <= 3 else '' }}>
      <summary>{{ plan['items']|length }} item(s){% if plan['items']|length > 3 %} — click to expand{% endif %}</summary>
      <ul class="items">
      {% for item in plan['items'] %}
        <li class="item-row {{ item.get('status', 'pending') }}">
          <div class="item-text">
            <span class="badge small item-badge {{ item.get('status', 'pending') }}">{{ item.get('status', 'pending') }}</span>
            <b>{{ item.subcategory }}</b> <span class="meta">({{ item.actionable }})</span>: {{ item.action }}
          </div>
          <form class="feedback item-feedback" method="post" action="{{ url_for('give_feedback') }}">
            <input type="hidden" name="plan_id" value="{{ plan.plan_id }}">
            <input type="hidden" name="href" value="{{ item.href }}">
            <button type="button" name="decision" value="accept">Accept</button>
            <button type="button" name="decision" value="reject">Reject</button>
          </form>
        </li>
      {% endfor %}
      </ul>
    </details>
    <form class="submit-plan-form" method="post" action="{{ url_for('submit_plan') }}"
          style="{{ '' if plan.get('ready_to_submit') else 'display:none' }}">
      <input type="hidden" name="plan_id" value="{{ plan.plan_id }}">
      <button type="submit" class="submit-plan"
              {{ 'disabled' if submit_state.running and submit_state.plan_id == plan.plan_id else '' }}>
        {{ 'Reassigning…' if submit_state.running and submit_state.plan_id == plan.plan_id else '✓ Submit' }}
      </button>
    </form>
  </div>
{% else %}
  <p class="empty">No plans yet — drop an export and run a cycle.</p>
{% endfor %}

<h2 class="section-title">Published output</h2>
<p><a class="link-card" href="{{ url_for('view_cited') }}">View raw cited.md &rarr;</a></p>

<h2 class="section-title">Recent activity <span class="count">last 15</span></h2>
<div class="card table-wrap">
<table class="log">
{% for e in session_log_tail %}
  <tr><td class="meta mono">{{ e.logged_at }}</td><td><span class="event">{{ e.event }}</span></td>
      <td class="mono">{{ e.extra }}</td></tr>
{% else %}
  <tr><td class="meta" colspan="3">No activity logged yet.</td></tr>
{% endfor %}
</table>
</div>

</main>

<script>
(function () {
  var alertsToggle = document.getElementById('alerts-toggle');
  var alertsDropdown = document.getElementById('alerts-dropdown');
  if (alertsToggle && alertsDropdown) {
    alertsToggle.addEventListener('click', function (e) {
      e.stopPropagation();
      alertsDropdown.classList.toggle('open');
    });
    alertsDropdown.addEventListener('click', function (e) { e.stopPropagation(); });
    document.addEventListener('click', function () {
      alertsDropdown.classList.remove('open');
    });
  }

  var form = document.getElementById('upload-form');
  var zone = document.getElementById('dropzone');
  var input = document.getElementById('export_file');
  var status = document.getElementById('dropzone-status');

  function submitUpload() {
    if (!input.files.length) return;
    status.textContent = 'Uploading ' + input.files[0].name + ' — starting a run…';
    form.submit();
  }

  if (form && zone && input) {
    input.addEventListener('change', submitUpload);

    ['dragenter', 'dragover'].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault();
        zone.classList.add('dragover');
      });
    });
    ['dragleave', 'drop'].forEach(function (evt) {
      zone.addEventListener(evt, function (e) {
        e.preventDefault();
        zone.classList.remove('dragover');
      });
    });
    zone.addEventListener('drop', function (e) {
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) {
        input.files = files;
        submitUpload();
      }
    });
  }

  var STATUSES = ['pending', 'accepted', 'rejected'];

  function rollup(planCard) {
    var statuses = Array.from(planCard.querySelectorAll('.item-row')).map(function (row) {
      return STATUSES.filter(function (s) { return row.classList.contains(s); })[0] || 'pending';
    });
    if (statuses.every(function (s) { return s === 'accepted'; })) return 'accepted';
    if (statuses.every(function (s) { return s === 'rejected'; })) return 'rejected';
    if (statuses.indexOf('pending') !== -1) return 'pending';
    return 'mixed';
  }

  document.querySelectorAll('form.item-feedback').forEach(function (fbForm) {
    fbForm.querySelectorAll('button[name="decision"]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var decision = btn.value;
        var newStatus = decision === 'accept' ? 'accepted' : 'rejected';

        var data = new FormData(fbForm);
        data.set('decision', decision);
        fetch(fbForm.action, { method: 'POST', body: data }).catch(function () {});

        var row = fbForm.closest('.item-row');
        var itemBadge = row.querySelector('.item-badge');
        row.classList.remove('pending', 'accepted', 'rejected');
        itemBadge.classList.remove('pending', 'accepted', 'rejected');
        row.classList.add(newStatus);
        itemBadge.classList.add(newStatus);
        itemBadge.textContent = newStatus;

        var planCard = fbForm.closest('.plan-card');
        var planStatus = rollup(planCard);
        var planBadge = planCard.querySelector('.plan-badge');
        ['pending', 'accepted', 'rejected', 'mixed'].forEach(function (s) {
          planCard.classList.remove(s);
          planBadge.classList.remove(s);
        });
        planCard.classList.add(planStatus);
        planBadge.classList.add(planStatus);
        planBadge.textContent = planStatus;

        var submitForm = planCard.querySelector('.submit-plan-form');
        if (submitForm) {
          // "mixed" counts too — it means every item was decided, just not
          // unanimously; the server-side pass drops/reassigns only the
          // rejected half and keeps the accepted half (see reevaluator.py).
          submitForm.style.display = (planStatus !== 'pending') ? '' : 'none';
        }
      });
    });
  });

  document.querySelectorAll('form.submit-plan-form').forEach(function (form) {
    form.addEventListener('submit', function () {
      var btn = form.querySelector('button.submit-plan');
      btn.disabled = true;
      btn.textContent = 'Reassigning…';
      // No preventDefault — a real page navigation follows, so the server-rendered
      // banner/meta-refresh (submit_state.running) takes over from here.
    });
  });
})();
</script>

</body>
</html>
"""

CITED_PAGE = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cited.md</title>
<style>
  :root { --bg: #f5f6fa; --surface: #ffffff; --border: #e3e5ea; --text: #1c1e21; --text-muted: #6b7280; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #14161a; --surface: #1d2025; --border: #2b2f36; --text: #e7e9ec; --text-muted: #9aa1ab; }
  }
  body { font-family: -apple-system, sans-serif; background: var(--bg); color: var(--text);
         max-width: 900px; margin: 2rem auto; padding: 0 1.25rem 3rem; }
  .back {
    display: inline-block; margin-bottom: 1.25rem; text-decoration: none; color: var(--text-muted);
    border: 1px solid var(--border); border-radius: 6px; padding: .35rem .7rem; font-size: .85rem;
  }
  .back:hover { color: var(--text); }
  pre {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 1.25rem; white-space: pre-wrap; word-wrap: break-word;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .85rem; line-height: 1.6;
  }
</style>
</head>
<body>
<a class="back" href="{{ url_for('dashboard') }}">&larr; back to dashboard</a>
<pre>{{ content }}</pre>
</body>
</html>
"""


@app.route("/")
def dashboard():
    plans = sorted(store.load_plans().values(), key=lambda p: len(p["items"]), reverse=True)
    snapshots = store.list_snapshots()
    return render_template_string(
        PAGE, plans=plans, run_state=_run_state, submit_state=_submit_state,
        current_policy=policy.load_current(), session_log_tail=_session_log_tail(),
        latest_snapshot=snapshots[0] if snapshots else None,
    )


def _start_run() -> None:
    if _run_lock.acquire(blocking=False):
        cancellation.reset()  # clear any cancellation left over from a previous pass
        _run_state["running"] = True
        threading.Thread(target=_run_in_background, daemon=True).start()


@app.route("/run", methods=["POST"])
def trigger_run():
    _start_run()
    return redirect(url_for("dashboard"))


@app.route("/upload", methods=["POST"])
def upload():
    # Disabled while anything else is running — the dropzone itself is replaced
    # by the processing view in that state (see PAGE template), so this is a
    # server-side backstop against a stale page/direct POST, not the primary guard.
    if _run_state["running"] or _submit_state["running"]:
        return redirect(url_for("dashboard"))
    file = request.files.get("export_file")
    if file and file.filename.endswith(".json"):
        config.ensure_dirs()
        file.save(config.DROP_DIR / file.filename)
        _start_run()  # dropping a file seeds the pipeline — no separate "run" click needed
    return redirect(url_for("dashboard"))


@app.route("/reset", methods=["POST"])
def reset_state():
    """Restore plans/cited.md/policy/taxonomy from the most recent automatic
    snapshot (see store.snapshot(), taken before every run cycle and plan
    submission). Refuses while something is actively running/submitting —
    restoring underneath a live background write would just get overwritten
    or race it; the button itself is hidden in that state too (see PAGE)."""
    if _run_state["running"] or _submit_state["running"]:
        return redirect(url_for("dashboard"))
    snapshots = store.list_snapshots()
    if snapshots:
        store.restore_snapshot(snapshots[0]["id"])
        _run_state["last_error"] = None
        _run_state["last_result"] = None
        _submit_state["last_error"] = None
        _submit_state["last_result"] = None
    return redirect(url_for("dashboard"))


@app.route("/cancel", methods=["POST"])
def cancel():
    """Cancel whatever's running — a full run or a plan submission. Best-effort:
    terminates the currently in-flight local-model subprocess immediately and
    sets the flag every loop checkpoint watches (see agent/cancellation.py)."""
    cancellation.request_cancel()
    return redirect(url_for("dashboard"))


@app.route("/alerts/clear", methods=["POST"])
def clear_alerts():
    """Dismisses the alerts dropdown's contents — otherwise the last run/submit
    result or error sits there forever (that was the actual complaint: these
    used to be permanent inline banners on every page load)."""
    _run_state["last_result"] = None
    _run_state["last_error"] = None
    _submit_state["last_result"] = None
    _submit_state["last_error"] = None
    return redirect(url_for("dashboard"))


@app.route("/feedback", methods=["POST"])
def give_feedback():
    plan_id = request.form["plan_id"]
    href = request.form["href"]
    decision = request.form["decision"]
    try:
        feedback.record_item(plan_id, href, decision)
    except (KeyError, ValueError):
        pass  # stale plan_id/href from a page that hasn't refreshed — ignore, dashboard reflects current state
    return redirect(url_for("dashboard"))


@app.route("/submit-plan", methods=["POST"])
def submit_plan():
    plan_id = request.form["plan_id"]
    if _submit_lock.acquire(blocking=False):
        cancellation.reset()
        _submit_state["running"] = True
        _submit_state["plan_id"] = plan_id
        threading.Thread(target=_submit_in_background, args=(plan_id,), daemon=True).start()
    return redirect(url_for("dashboard"))


@app.route("/cited")
def view_cited():
    content = config.CITED_MD.read_text() if config.CITED_MD.exists() else "(no cited.md yet — run a cycle first)"
    return render_template_string(CITED_PAGE, content=content)


if __name__ == "__main__":
    config.ensure_dirs()
    app.run(host="127.0.0.1", port=5000, debug=False)
