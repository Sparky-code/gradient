# ROADMAP — where this goes next

This isn't `GAPS_AND_FILL.md`. That doc tracks bugs and unfinished pieces of what's already
been decided; the "Fill" column is always mechanical. This doc is the opposite kind of list —
three real directions the project should grow in, none of which are "finish the code," all of
which need actual design thought before implementation starts. Ranked by dependency order, not
importance: #2 is cheap and unblocks trust in what's already built, #1 is foundational (the
project can't fully claim local-first while it does), #3 is the actual point of doing #1 and #2
at all.

---

## 1. Decouple from Senso — ✅ DONE

**Status: shipped.** `agent/adapters/senso.py` is deleted, `agent/adapters/vectorai.py` now has
`ground_locally()`/`ground_locally_many()`, both call sites below are swapped, and the `API.md`
key is removed. See `RUNBOOK.md` §5/§10 and `GAPS_AND_FILL.md`'s resolved "Senso was a redundant
hosted grounding dependency" row for the implementation record — the reasoning below is kept
as-written since it's still the accurate "why," not just history. Item 5's open question is the
one part of this section still genuinely unresolved.

### What Senso did before this

Two call sites, both in the hot path, per `RUNBOOK.md` §5 and §8:

- `senso.ingest_post()` — pushes every classified post's text into Senso's hosted KB
  (`POST /org/kb/raw`), called from `loop._ingest_new_posts_into_senso()` for every new
  high-quality post.
- `senso.ground()` — queries that KB back (`POST /org/search/context`) for citation chunks,
  called from two places: the `senso-grounder` agent (one call per plan, grounding an
  *interest* — "hiking trails and outdoor destinations" — in supporting context) and
  `taxonomy_evolver.evolve()` (grounding a *candidate new category* before promoting it, so a
  taxonomy promotion isn't just a clustering artifact — it has real supporting text behind it).

Both were real, live, working integrations — this wasn't a "replace a stub" gap like Guild or
Replay.io was. The reason to decouple wasn't that Senso was fake; it's that it was an external
hosted dependency for something this project already had the raw material to do itself.

### Why this is actually redundant, not just undesirable

Look at what Senso's KB actually contains: the exact same post content that's *also* being
embedded and stored in VectorAI DB (`vectorai.remember_posts()`, self-hosted, already running).
Two systems are independently ingesting the same corpus — one hosted and search-queried over
HTTP, one local and already doing real semantic search for episodic recall
(`recall_similar_many()`). Senso's `ground()` is answering "what supporting context exists for
this interest?" with a full-text search over a KB that's 1:1 with content VectorAI DB already
has embedded. That's not two different capabilities; it's the same retrieval-augmented-grounding
pattern implemented twice, once locally and once against a hosted API.

There's a sharper version of this argument specific to what this product actually is (see §3
below): grounding an interest plan in *the user's own saved posts* is arguably more honest and
more useful than grounding it in Senso's general KB content, since Senso's KB here only ever
contains what this pipeline itself pushed into it — there is no independent external corpus
being searched. The "citations" a person sees today aren't third-party sources; they're their
own other posts, reflected back through a hosted round-trip. That's worth doing locally on its
own merits, not just as a decoupling exercise.

### What's needed to fill the gap

1. ✅ **A local grounding function** — `vectorai.ground_locally()`/`ground_locally_many()`,
   shaped like Senso's old `ground()` return (`{"grounded": bool, "citations": [str, ...],
   "source": str}`). Turned out `remember_posts()`'s existing payload (`subcategory`/`action`)
   was already enough for a citation-quality line — no `content` field or schema change needed.
2. ✅ **Swapped the grounding agent's handler** — renamed `senso-grounder` → `vectorai-grounder`
   in `orchestrator.build_default_coordinator()`. Went further than "same shape, same dispatch":
   the old agent dispatched once *per plan* (fine when the underlying call was a cheap Senso HTTP
   request); the new one batches every new plan's interest into one `ground_locally_many()` call,
   matching `vectorai-recaller`'s existing batching discipline — a local embed subprocess call
   costs ~10s regardless of batch size, so doing it once per plan would have reintroduced the
   exact per-item-model-load anti-pattern this codebase has fixed elsewhere.
3. ✅ **Swapped `taxonomy_evolver.evolve()`'s grounding step** — a single-item `ground_locally()`
   call, excluding the candidate cluster's own member hrefs so it has to find genuinely other
   supporting content, not just echo the cluster back at itself.
4. ✅ **Decided what happens to `senso.ingest_post()` — cut it, not kept for a transition.**
   Once `ground()` no longer reads from Senso's KB, ingesting into it had no downstream consumer.
   Removed alongside `ground()`'s swap: the write, its `senso_ingested.json` dedup file, and the
   tracking function in `loop.py` are all gone.
5. **Open question, not yet answered: is external grounding worth adding back later, deliberately
   this time?** Decoupling from Senso removes the *only* source of context outside what the user
   themselves saved. If "ground this interest in real information beyond your own posts" turns
   out to matter for the product (see §3's learning-plan idea — a learning plan probably *should*
   pull in real external material, not just reflect your own saved posts back at you), that's a
   deliberate future integration, scoped on its own terms — not a reason to keep Senso around
   as a placeholder for it now.

---

## 2. A real interface for what happens to items as they're sorted

### The actual gap

Two enrichment mechanisms exist and produce real data, but only one of them is visible anywhere
a person would look:

- **Tags** (`agent/tagger.py`, `_tag_worker.py`) — 3-6 concrete keywords per item, generated
  once a plan resolves. Stored on the item (`item["tags"]`). ✅ Now rendered in both `cited.md`
  (`tags_suffix` in `publisher.py`) and `webui.py`'s dashboard (chip badges under the action
  line, `webui.py`'s plan-card template) — shipped in `cecf752`, ahead of this doc catching up.
- **Reassignment history** (`agent/reevaluator.py`) — when a rejected item gets moved to a
  better-fitting category, the move is logged (`session_log`'s `plan_reevaluated` event carries
  `{"reassigned": [{"href", "to", "score"}, ...]}`) but that's a write-only audit trail. The item
  itself, once moved, carries no memory of where it came from or why — `_upsert_into()` /
  `_strip_transient()` in `reevaluator.py` explicitly drop the old `status`/`category` when
  moving an item to its new plan. If a person looks at an item sitting in "food and cooking" and
  wonders "wait, was this originally filed somewhere else?", the honest answer is: that
  information exists, but only by grepping `session_log.jsonl` for the href, which is not
  something the product should require of anyone.

Put simply: the pipeline has a real, evolving audit trail of every decision it makes about an
item, and most of it still doesn't survive past a JSONL line nobody is meant to read.

### What to build

1. ✅ **Surface tags in the dashboard — DONE.** `item.tags` renders as a small chip list under
   the action line in `webui.py`, matching what `cited.md` already did in markdown.
2. ✅ **Give each item a real `history` field — DONE.** Appended to (never overwritten) at each
   enrichment event: `{"event": "tagged" | "reassigned" | "orphaned", "at": <timestamp>,
   "from_plan": <id|null>, "score": <float|null>, "reason": <str|null>}`. `reevaluator.py` writes
   this — `_upsert_into()`/`_strip_transient()` used to strip context when moving an item; they now
   carry a `history` entry forward describing the move that just happened.
3. ✅ **An item detail view — DONE.** `GET /item/<href>` in `webui.py` (linked from each item row's
   new "details" link) shows: current tags, the full `history` timeline, taxonomy promotion
   evidence (only when this exact item's href is in the promoted category's own `cluster_hrefs` —
   not just "this item currently sits in a category that was ever promoted"), and this item's
   plan's grounding citations + VectorAI DB recall hits — both already live from §1, now surfaced
   next to the item's own history instead of only in `cited.md`. Looked up by `href` (not
   `plan_id`) so the permalink survives a reassignment.
4. ✅ **A taxonomy view — DONE.** `GET /taxonomy` in `webui.py` (linked from a new Taxonomy stat
   card on the dashboard, mirroring the existing Policy card) reads `taxonomy.load_current()` and
   shows seeded categories separately from auto-promoted ones, each promotion's description,
   cluster size, member posts (linked to their item detail pages from §2.3), and grounding
   citations — newest promotion first. Along the way, both this view and the §2.3 item detail
   view were fixed to fall back to the pre-decoupling `senso_citations` evidence key (real
   categories promoted before §1's Senso→VectorAI rename still carry the old field name; without
   the fallback their citations silently rendered as an empty list).

**§2 is now fully done** — tags, item history, the item detail view, and the taxonomy view are
all live in `webui.py`.

---

## 3. Make the sorted knowledge actionable — the actual point

Right now, a resolved item's life ends at "sits in a plan, correctly categorized, tagged,
citable." That's a real, working curation loop — but curation alone caps out at "a nicer list."
The self-evolving claim this project makes is currently spent entirely on *getting the sorting
right*, not on *doing something with the sorting once it's right*. That's the gap worth closing
next, once §1 and §2 give it a clean, visible foundation to build on.

Three concrete directions, roughly in order of how directly they're already supported by data
this pipeline already produces:

### Self-profiling — ✅ DONE

**Status: shipped.** The original text below assumed VectorAI DB's episodic memory
(`remember_posts()`/`update_status()`) held enough to aggregate directly — checked while
implementing, and it doesn't: `remember_posts()`'s payload is only `href/category/subcategory/
actionable/action/status`, no tags, no timestamps, and `update_status()` overwrites `status` in
place with no history. The real, complete source of truth turned out to already be local and
already sufficient: `plans.json` (current category/tags/status per item). A profile can be built
from that alone, no new VectorAI infrastructure needed.

Three decisions were made explicitly before writing any code (see conversation record — this
paragraph is the durable summary):
1. **Scope this pass: self-profiling only**, not learning plans or self-directed next actions —
   both of those still depend on open questions below (external grounding for learning plans;
   both self-profiling and learning plans existing first for next-actions).
2. **Architecture: cache to `data/state/profile/current.json`, recomputed via a `profiler`
   orchestrator agent** — not policy.py/taxonomy.py's versioned-promotion pattern (a profile has
   no discrete "promotion" event, just a full recompute every time), and not purely on-demand
   either, even though the aggregation needs no local-model call and would be cheap enough to
   compute live at page-load. The agent is dispatched once per `run_once()` pass (not per drop
   file — it aggregates ALL plans, not one file's posts), and `agent/profile.py`'s `recompute()`
   is also called directly (bookkeeping-call style, same precedent as the VectorAI-remember write
   in `loop.py`) from `agent/feedback.py` (`record()`, `record_item()`) and
   `agent/reevaluator.py` (`reevaluate_plan()`) — everywhere plans.json actually changes, not just
   the drop-file pipeline, so the cached profile never goes stale behind a click.
3. **Content: descriptive only, first version** — category/tag frequency, accept/reject rate per
   category, tag co-occurrence (pairs seen together more than once, to filter one-off noise). The
   sharper cross-check this section originally floated — flagging categories the user keeps
   rejecting that `policy.py`'s suppression should already be catching — is deliberately deferred
   to a follow-on now that the descriptive view exists to build it on top of.

Implementation: `agent/profile.py` (`compute()`/`recompute()`/`load_current()`), a `profiler`
agent in `orchestrator.py`, dispatch from `loop.py`'s `run_once()`, direct `recompute()` calls
from `feedback.py`/`reevaluator.py`, a `GET /profile` view + dashboard stat card in `webui.py`,
and `profile_current.json` added to `store.py`'s snapshot/restore targets.

### Learning plans

`actionable` values like `how_to` and `wishlist_place` are already a signal of *intent to act*,
not just *interest*. A cluster of `how_to` items under "coding and software tools," each tagged
with concrete tools/techniques, is most of the way to a sequenced learning path already — what's
missing is a pass that takes a resolved, tagged cluster and orders it (prerequisite tags before
dependent ones, breadth-first survey items before deep-dive items) rather than leaving it as a
flat bulleted list. This is the one direction where the §1 open question about external grounding
matters most: a learning plan built only from what someone already saved is inherently limited to
what they already knew enough to save — pulling in real external material is probably necessary
for a learning plan to be more useful than the raw saved posts it's built from.

**§1's external-grounding question is answered: yes, pull in real external material.** What that
actually needs (researched, not guessed — see conversation record for the full comparisons):

1. **Retrieval backend — still being decided, deliberately stubbed.** Senso, as this repo
   originally used it, was never external grounding — it was a bring-your-own-corpus KB (push the
   user's own posts in, search them back out), the same capability VectorAI DB now does locally.
   Real external grounding needs a different class of tool: a live web-search/retrieval API. Three
   real candidates compared, no clean winner: **Exa** (neural/semantic search, full page text via
   `contents.text`, free tier 1,000/mo — best fit for fuzzy "find a good tutorial about X" queries,
   but raised pricing $5→$7/1k in March 2026, an early tightening signal), **Tavily** (RAG-shaped
   output, free tier 1,000 credits/mo, but documented stale/cached-link complaints that cut against
   "cite genuinely external sources" being the point, plus Nebius's Feb 2026 acquisition as a
   vendor-risk flag), **self-hosted SearXNG** (zero cost, zero vendor, truest to local-first, but a
   real and actively-discussed operational burden — backend search engines individually blocking a
   fresh instance via CAPTCHA/fingerprinting, not a "docker compose up and forget" service like the
   existing VectorAI container). Google CSE and Bing Search API are dead ends (closed to new users
   / retired). Landing recommendation was Exa primary + SearXNG fallback, matching this project's
   own Pioneer precedent (real call + honestly-documented failure + a real, not stub, local
   fallback) — final pick deferred pending further research.
2. **Source-quality composite — decided: build the full version.** No true automatable "industry
   standard" exists for general web pages (E-E-A-T has no API, CRAAP is a human-judgment
   checklist, NewsGuard is sales-gated, citation metrics only cover academic papers). The
   composite: structural metadata heuristics (HTTPS, byline, about page, recency — free), an
   LLM-as-judge rubric pass reusing the local model already running for tagging (approximates the
   human-judgment parts of CRAAP/E-E-A-T no API can give you), and cross-source corroboration
   (does more than one independent search result agree — nearly free since a multi-result search
   call already returns it).
3. ✅ **Trust curation — DONE: mechanical auto-evolve only, framed honestly as usage-survival.**
   Full design rationale and mechanics: **[docs/SOURCE_TRUST.md](docs/SOURCE_TRUST.md)**. Summary:
   real systems draw a hard line — *behavioral* trust signals auto-evolve fine with no human gate
   (Spamhaus, Safe Browsing, Stack Overflow reputation — reversible, non-adversarial, matches this
   project's own `taxonomy_evolver.py` pattern), but *factual-reliability* judgments stay
   human-gated in every real system studied (NewsGuard, MBFC, Wikipedia's Reliable Sources
   noticeboard — adversarial, consequential, unlike a wrongly-named category). Landing design: a
   domain that survives `PROMOTION_STREAK` (3, matching `CLUSTER_MIN_SIZE`) clean appearances
   auto-promotes to a "trusted" tier, demotes immediately on a single incident, and is documented
   everywhere as NOT a factual-reliability claim — just "hasn't caused a problem yet." A real
   human-review step was considered and set aside for now, not ruled out permanently.

**Implemented so far**:
- `agent/adapters/external_search.py` — the retrieval contract every backend (real or stub) must
  satisfy: `search(query, max_results) -> {"grounded", "source", "results": [{"url", "title",
  "excerpt", "raw_content", "published_date"}], "reason"}`. `raw_content`/`published_date` are the
  two fields that vary by vendor (Exa/Tavily return full text and a date, SearXNG doesn't) —
  deliberately optional so downstream scoring works off `excerpt`/`url` alone. Currently returns
  the stub shape unconditionally (no backend chosen yet) — same real-call-vs-local-fallback split
  as `pioneer_api.py`/`pioneer.py`, just not yet past the "which real call" decision.
- `agent/source_quality.py` + `agent/_source_quality_worker.py` — the full composite decided
  above, built entirely against `external_search.py`'s contract (vendor-agnostic, not blocked on
  the retrieval decision): `_structural_score()` (HTTPS/byline/about-page/date, free/deterministic),
  `_corroboration_flags()` (Jaccard keyword overlap between different-domain results, pure Python),
  and an LLM-judge subprocess worker (same isolation pattern as `_tag_worker.py`, CRAAP/E-E-A-T-
  inspired rubric prompt) combined into one `score_sources()` entry point. Deliberately does NOT
  reuse the classification LoRA adapter (`agent/adapters/lora.py`) — that's trained on Instagram-
  post accept/reject signal, an unrelated judgment task to source credibility.

- `agent/source_trust.py` — the usage-survival registry above, keyed on `source_quality.py`'s
  automated composite today (no learning-plan consumer exists yet to supply real accept/reject
  feedback), with an explicit `feedback` seam for that to plug in once one does. No caller yet —
  see `docs/SOURCE_TRUST.md`'s "what's genuinely still open" section.

The actual cluster-ordering pass (the learning-plan feature itself, the thing that would call
`external_search.search()` → `source_quality.score_sources()` → `source_trust.record_pass()` in
sequence) is still open, and is the only piece left blocked on the retrieval vendor decision.

### Self-directed next actions

The narrowest, most product-shaped version of this: use the profile (now live, see above) to
proactively surface "you have 6 saved posts about X, all accepted, none acted on yet — want to
turn this into a plan?" This is the piece that would make `cited.md` (or its dashboard
equivalent) feel like it's working *for* the person between sessions, not just reporting what
happened last session. Depends on both of the above existing first — there's no "next action" to
suggest without a profile to notice the pattern (now built) and no way to act on it without the
learning-plan structuring to turn it into (not yet built).

### What learning plans + self-directed next actions still need, concretely

- **Resolve §1's still-open external-grounding question first** — a learning plan built only from
  what someone already saved is inherently limited to what they already knew enough to save;
  whether that's acceptable or whether a genuinely-scoped external grounding integration is worth
  adding is a real design decision, not an implementation detail, and should happen before a
  learning-plan pass gets written.
- Likely a new agent in `orchestrator.py`'s Coordinator for the learning-plan ordering pass (a
  `planner-v2` role, matching the `profiler` precedent — "new capability = new named agent"
  rather than bolting this onto `loop.py` directly).
- A new dashboard surface for learning plans — not something `cited.md`'s per-plan bullet format
  can absorb; a sequenced learning path is a different shape of artifact than "a list of posts,"
  and deserves its own view, same reasoning that gave the profile its own `/profile` page rather
  than squeezing it into the existing dashboard.

---

## Sequencing

**§1 shipped out of order** relative to the original plan below (it was tackled before §2), which
turned out fine — it was self-contained and didn't depend on §2's UI work landing first. **§2 is
done** — it was the cheapest of the three, carried no design risk, and makes every other change in
this roadmap (§1's local grounding and §3's profile alike) something a person can actually *see*,
rather than another thing that only shows up in a JSONL file. **§3 is in progress**: self-profiling
(the lowest-lift of its three sub-directions) is done, landing on top of §1+§2's foundation exactly
as planned (tags, history, item detail, and taxonomy view all feeding into a visible, trustworthy
base rather than another opaque process). **Learning plans and self-directed next actions are
next, and last**, deliberately — still the least scoped part of this roadmap (the learning-plan
ordering logic and §1's still-open external-grounding question both need real design decisions
before implementation starts), and now build on a real profile instead of a hypothetical one.
