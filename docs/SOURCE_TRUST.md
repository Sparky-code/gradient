# SOURCE TRUST — how external source curation works, and why it's shaped this way

See also: **[RUNBOOK.md](../RUNBOOK.md)** (architecture + honest real-vs-stub audit),
**[ROADMAP.md](../ROADMAP.md)** §3 (where this fits — "learning plans," the feature this
registry will eventually serve), and `agent/source_quality.py` (the composite scorer this
registry consumes) / `agent/source_trust.py` (the implementation this doc describes).

**Status: the registry itself is implemented and tested (`agent/source_trust.py`,
`tests/unit/test_source_trust.py`). The feature that would call it — the learning-plan
cluster-ordering pass — is not built yet.** This module has no caller in the pipeline today.
It exists so that once the learning-plan feature does call `agent/source_quality.score_sources()`
on real external search results, there's already a considered, tested trust layer sitting under
it, rather than that feature inventing one under time pressure.

---

## The one-line version

A domain that keeps scoring cleanly across independent appearances auto-promotes to a
**"trusted"** tier with no human approval step — the same no-human-gate pattern already used for
`agent/taxonomy_evolver.py` (new categories) and `agent/export_type_evolver.py` (new export
types). A single bad showing demotes it immediately, no grace period. And critically:
**"trusted" here never means "verified reliable."** It means "hasn't caused a problem yet." That
distinction is the entire design — see below for why it matters enough to spell out.

---

## Why a no-human-gate registry is defensible here, and where it stops being defensible

This project's whole autonomy claim rests on **no manual intervention between passes** — policy
promotion, taxonomy promotion, export-type promotion all auto-apply with no approval step. It
would be inconsistent to suddenly require a human for source trust *without a reason*. So the
question this design had to answer honestly was: is source trust actually the same kind of thing
as a new taxonomy category, or is it a different kind of thing wearing a similar shape?

Research into how real systems handle this (see the conversation record for the full comparison)
found a consistent, sharp dividing line:

- **Mechanical/behavioral trust signals auto-evolve just fine, with no human gate, in every real
  system that uses them.** Spamhaus's DBL lists and delists mail domains automatically based on
  abuse-pattern matching — a human only enters on *appeal*, not on the routine promote/demote
  path. Google Safe Browsing's malware/phishing classifiers list and unlist automatically the
  same way. Stack Overflow's reputation system is *fully* automated — every vote mechanically
  adds or subtracts points, no moderator reviews each one. What all three have in common: the
  signal being aggregated is **behavioral** (did this thing keep misbehaving, did people keep
  voting it up), the domain is **reversible** (a wrongly-listed domain gets delisted the moment
  its behavior improves), and there's comparatively **low blast radius** if the automation is
  wrong for a while.

- **Factual-reliability judgments stay human-gated in every real system studied**, even ones that
  would clearly benefit from automating it away. NewsGuard grades sources on 9 journalistic-
  practice criteria, but every score requires a trained analyst plus a senior editor plus a
  co-CEO sign-off before it publishes — explicitly not ML-scored. Media Bias/Fact Check's own
  methodology page disclaims itself as "not a tested scientific method" and keeps human
  researchers in the loop. Wikipedia's Reliable Sources noticeboard requires an actual multi-editor
  discussion (an RfC) to move a source between reliability tiers — it's a discourse artifact, not
  a number that updates itself. The reason all three stayed human-gated despite the obvious cost
  isn't inertia — it's that **misinformation is adversarial** (bad actors actively try to game
  reputation systems, unlike a mail domain that's just genuinely sending spam or not) and
  **consequential in a way that's hard to reverse** (a person who acted on a bad source already
  acted on it; delisting the domain afterward doesn't undo that).

A new taxonomy category is squarely in the first bucket: behavioral (do posts keep clustering
here), reversible (a bad category just sits mostly-empty and gets ignored), low blast radius (at
worst, a person sees one oddly-named plan). **Whether a cited external source is actually
trustworthy is squarely in the second bucket** — the thing this project would be asserting isn't
"this domain keeps showing up," it's "you can believe what this domain says," and that's exactly
the adversarial, consequential judgment every real system studied keeps a human in the loop for.

## The resolution: automate the first bucket, and refuse to pretend it's the second

This registry deliberately automates *only* the mechanical, behavioral layer — and is explicit,
everywhere it surfaces, that it is not making the factual-reliability claim a person might assume
"trusted" implies. Concretely:

- What gets tracked per domain is **"how many times in a row has this domain's automated
  quality composite (`agent/source_quality.py` — structural heuristics + LLM-as-judge +
  cross-source corroboration) come back clean,"** not "is this domain actually correct about
  anything." A domain can be well-structured, written by an identifiable author, and
  corroborated by other sources, and still be wrong about a specific claim — the registry has no
  way to know that, and doesn't claim to.
- The tier name in the code and this doc is **"trusted," not "verified" or "reliable"** —
  deliberately. "Trusted" here should be read the way Spamhaus's absence-from-a-blocklist should
  be read: an absence of a bad signal, not the presence of a good one.
- **Demotion has no grace period.** The moment a previously-trusted domain produces one incident
  (a composite score below `CLEAN_THRESHOLD`, or an explicit human rejection once that signal
  exists), it drops back to `unverified` immediately and its clean streak resets to zero — see
  Spamhaus's own auto-relist behavior above. A multi-pass trust history buys a domain nothing once
  it produces a single bad showing; it has to earn the streak back from scratch.
- If this project ever *does* want the stronger claim — "this source has actually been judged
  reliable, not just consistently well-formed" — the honest thing to do is what NewsGuard/MBFC/
  Wikipedia all did: add a real human review step for that specific claim, not stretch the
  mechanical registry to imply something it can't actually verify. That option was considered and
  set aside for now (see ROADMAP.md §3's decision record), not ruled out permanently.

---

## How it actually works

### The signal it's keyed on today, and the seam for a better one later

The ideal signal, per the original design intent, is a *person's* real accept/reject decision on
a learning plan that cited a given source — actual usage-survival, not just repeated automated
scoring. But that signal doesn't exist yet: the learning-plan feature that would produce it isn't
built. Rather than invent a fake feedback loop or block this module on a feature that doesn't
exist, `record_pass()` is keyed on the best signal that *does* exist today —
`agent/source_quality.score_sources()`'s own automated composite — with an explicit seam
(`record_pass(scored_results, feedback=None)`) for a real consumer to supply actual
accept/reject decisions later:

```python
decision = feedback.get(result["url"])
if decision == "rejected":
    is_clean = False
elif decision == "accepted":
    is_clean = True
else:
    is_clean = result["quality"]["overall"] >= CLEAN_THRESHOLD  # today's only path
```

An explicit human decision always overrides the automated score when it exists — a person
rejecting a source is a stronger, more consequential signal than any composite heuristic, and the
code treats it that way. Until the learning-plan feature exists to supply that `feedback` map,
every domain goes through the automated-only branch — this is stated plainly here rather than
left implicit, because pretending today's signal is already "real usage" would undercut the exact
honesty standard the rest of this project holds itself to (see RUNBOOK.md's own tone throughout).

### The mechanics

- **`CLEAN_THRESHOLD = 0.6`** — the bar a domain's `quality.overall` composite must clear (or
  exceed) on a given appearance to count as clean. Picked as a plain midpoint-and-above cutoff on
  a 0–1 composite that's itself an average of two 0–1 signals (structural + LLM-judge) plus an
  optional corroboration bump — not tuned against real data yet (none exists), and worth
  revisiting once real learning-plan usage produces actual outcomes to check it against.
- **`PROMOTION_STREAK = 3`** — a domain needs 3 clean appearances *in a row* to promote. Matches
  `CLUSTER_MIN_SIZE = 3` in `taxonomy_evolver.py`/`export_type_evolver.py` exactly, on purpose —
  same underlying judgment call ("is this genuine recurrence, not a one-off"), same number,
  applied to a different kind of recurrence.
- **One pass per domain per batch, not per result.** `record_pass()` is meant to be called once
  per external search (one call to `agent/adapters/external_search.search()`, scored by
  `score_sources()`). If that single search returns two different pages from the same domain,
  they're deduped down to one pass for that domain — the streak counts independent *appearances
  across searches*, not raw result count within one search.
- **An incident always resets the clean streak to zero**, whether the domain was trusted or not.
  If it was trusted, it also demotes immediately (`tier` flips back to `"unverified"`,
  `demoted_at` is stamped) — no partial credit for the streak it had built.
- **Every promotion and demotion is appended to a `history` list** inside the same
  `current.json` (not separate versioned files, unlike `policy.py`/`taxonomy.py` — there's no
  single "current version number" that makes sense for a registry tracking many independent
  domains at once; a flat append-only event log is the more honest shape here). Each entry
  records the domain, the event, the timestamp, and the streak/incident count at that moment —
  so "why was this domain trusted" is always answerable from the state file itself, matching this
  project's existing standard of keeping real evidence next to every auto-promoted artifact.

### Where the state lives

`data/state/source_trust/current.json` — `{"domains": {<domain>: {...}}, "history": [...]}`.
Included in `store.py`'s snapshot/restore targets alongside plans/policy/taxonomy/export-types/
profile, so a `/reset` to a prior snapshot restores trust state consistent with everything else
from that point in time, not left dangling ahead of a rolled-back `plans.json`.

---

## What's genuinely still open

- **No caller yet.** This registry has no consumer — the learning-plan cluster-ordering pass
  that would call `agent/adapters/external_search.search()`, score results via
  `agent/source_quality.score_sources()`, and record them via `source_trust.record_pass()` isn't
  built. `is_trusted()`/`tier_for()` exist for that future consumer to use — e.g., to skip
  re-running the (comparatively expensive) LLM-judge pass on a domain that's already proven
  itself, once that optimization is worth making.
- **`CLEAN_THRESHOLD` and `PROMOTION_STREAK` are reasoned defaults, not tuned values** — there's
  no real usage data yet to tune them against. Worth a genuine benchmarking pass (see
  `docs/BENCHMARKS.md`'s own "planning doc, not measured yet" precedent) once the learning-plan
  feature is producing real passes to observe.
- **The human-review option is deferred, not rejected.** If misinformation risk in a learning
  plan turns out to matter more in practice than this design currently assumes, the honest
  escalation path — matching NewsGuard/MBFC/Wikipedia — is a real review step specifically for
  factual-reliability claims, layered on top of (not replacing) this mechanical registry.
