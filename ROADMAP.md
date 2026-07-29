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
4. **A taxonomy view** — separate from any one item: `taxonomy.load_current()` already has every
   promoted category's version history and promotion evidence; there's no UI for it at all today,
   only `data/state/taxonomy/current.json` and `vN.json` files. A person should be able to see
   "here are the categories this agent has invented for you, and why," which is a genuinely
   interesting artifact of the self-evolving claim and currently completely invisible.

---

## 3. Make the sorted knowledge actionable — the actual point

Right now, a resolved item's life ends at "sits in a plan, correctly categorized, tagged,
citable." That's a real, working curation loop — but curation alone caps out at "a nicer list."
The self-evolving claim this project makes is currently spent entirely on *getting the sorting
right*, not on *doing something with the sorting once it's right*. That's the gap worth closing
next, once §1 and §2 give it a clean, visible foundation to build on.

Three concrete directions, roughly in order of how directly they're already supported by data
this pipeline already produces:

### Self-profiling

The raw material already exists and is already real: VectorAI DB's episodic memory
(`vectorai.remember_posts()` / `update_status()`) has every post's category, subcategory, tags,
and actual accept/reject outcome, accumulating pass over pass. Nobody has ever aggregated it into
a profile. A person's real, evolving interest graph — which categories keep recurring, which
ones they consistently reject despite the agent kept re-surfacing them (a genuine signal the
suppression policy should already be catching, worth cross-checking against `policy.py`'s
exemplars), which tags cluster together across categories — is sitting in state this system
already writes, just never read back as a *profile* rather than a transaction log. This is the
lowest-lift of the three: it's a new read/aggregation pass over existing data, not a new kind of
data collection.

### Learning plans

`actionable` values like `how_to` and `wishlist_place` are already a signal of *intent to act*,
not just *interest*. A cluster of `how_to` items under "coding and software tools," each tagged
with concrete tools/techniques, is most of the way to a sequenced learning path already — what's
missing is a pass that takes a resolved, tagged cluster and orders it (prerequisite tags before
dependent ones, breadth-first survey items before deep-dive items) rather than leaving it as a
flat bulleted list. This is the one direction where the §1 open question about external grounding
matters most: a learning plan built only from what someone already saved is inherently limited to
what they already knew enough to save — pulling in real external material (a genuinely-scoped
grounding integration, not Senso-as-placeholder) is probably necessary for a learning plan to be
more useful than the raw saved posts it's built from.

### Self-directed next actions

The narrowest, most product-shaped version of this: use the profile (above) to proactively
surface "you have 6 saved posts about X, all accepted, none acted on yet — want to turn this into
a plan?" This is the piece that would make `cited.md` (or its dashboard equivalent) feel like it's
working *for* the person between sessions, not just reporting what happened last session. Depends
on both of the above existing first — there's no "next action" to suggest without a profile to
notice the pattern and no way to act on it without the learning-plan structuring to turn it into.

### What this needs, concretely

- A new data surface: whatever the self-profile actually is (probably a `data/state/profile.json`
  or a new VectorAI DB collection aggregating across the episodic memory rather than living in it)
  — this is a design decision, not an implementation detail, and should happen before any code
  gets written for it.
- Likely a sixth agent in `orchestrator.py`'s Coordinator (a `profiler` or `planner-v2` role),
  keeping the existing pattern of "new capability = new named agent" rather than bolting this onto
  `loop.py` directly.
- A new dashboard surface entirely — this is not something `cited.md`'s per-plan bullet format can
  absorb; profiles and learning plans are a different shape of artifact than "a list of posts,"
  and deserve their own view rather than being squeezed into the existing one.

---

## Sequencing

**§1 shipped out of order** relative to the original plan below (it was tackled before §2), which
turned out fine — it was self-contained and didn't depend on §2's UI work landing first. **§2 is
in progress** — cheapest of what's left, no design risk, and makes every other change in this
roadmap (including §1's local grounding, now live, and §3's profile) something a person can
actually *see*, rather than another thing that only shows up in a JSONL file. Tags (2.1),
`history` (2.2), and the item detail view (2.3) are all live; only the taxonomy view (2.4) is
left. **§3 last**, deliberately —
it's the most valuable direction long-term but also the least scoped right now (the
self-profile's data shape, the learning-plan ordering logic, and §1's still-open
external-grounding question all need real design decisions before implementation starts), and
doing it on top of §1+§2 instead of underneath them means it inherits a visible, trustworthy
foundation instead of another opaque process nobody can see the reasoning behind.
