# What `cited.md` actually is

`cited.md` is the one artifact a person is meant to actually read. Everything
else in this system — the drop folder, `plans.json`, the taxonomy store, the
local session log, the five local agents — exists to produce it and to react to
what a person does with it. If Gradient were shipped as a product, `cited.md`
(or its web-dashboard equivalent) is the product; the rest is infrastructure.

## The one-line pitch

You export your Instagram saves. Gradient turns "600 things I meant to look at
someday" into a small number of interest plans — grouped, deduplicated,
summarized, and **every claim traced back to a real source** — that you can
accept, reject, or ignore, and that get measurably better each time you do.

## Why "cited"

Because nothing in it is asserted without a receipt. Three separate citation
mechanisms feed into the file, and they answer three different questions:

| Mechanism | Question it answers | Where it shows up |
|---|---|---|
| `source: <href>` | Where did this actually come from? | Every single item, no exceptions |
| Senso grounding | Is there real supporting context for this *interest as a whole*, beyond one post? | `_Senso-grounded citations:_` under a plan, pulled from Senso's knowledge base via `POST /org/search/context` |
| VectorAI DB recall | Has something like this come up before, and what did I decide about it last time? | `🧠 VectorAI DB recall — similar past posts:` — real semantic search over prior accept/reject history |

That third one is the interesting one for a "self-evolving" pitch: it's not
just citing external sources, it's citing *your own past decisions* back to
you. A new "home coffee roasting" post surfacing next to "you rejected
something almost identical to this three weeks ago" is the file citing your
own feedback loop as evidence.

## Anatomy of the file

Reading top to bottom, in the order `agent/publisher.py` actually writes it:

1. **Header** — generation timestamp, plan count, and how many posts were
   filtered out as low-signal before ever reaching a plan (the taxonomy/policy
   layers doing their job silently, surfaced as a number so it isn't invisible).
2. **One section per interest plan**, sorted by size (biggest interest first —
   the thing you have the most saved posts about is probably the thing worth
   your attention first). Each section header carries a status emoji
   (⏳ pending / ✅ accepted / ❌ rejected / 🔀 mixed) and the plan's internal
   id, so a person and the CLI/dashboard are always talking about the same
   object.
3. **One bullet per item** inside a plan — subcategory, actionable type
   (`wishlist_place`, `how_to`, `recipe`, etc.), status, tags (from the
   tagger pass, added when an item gets reassigned), the action itself, any
   extracted key facts, the source link, and — if this pass found them —
   Senso citations and VectorAI recall hits.
4. **A feedback instruction line** per plan, telling you exactly how to act
   on it (dashboard buttons, or the equivalent CLI command) — the file
   doesn't just report, it hands you the next action.

There used to be a fifth line here — a stubbed x402/CDP paywall notice
(`payments.PAYWALL_NOTICE`) — sitting right below the header. It's gone:
monetization was decided to be out of scope for what this agent actually
does, and `agent/adapters/payments.py` was deleted rather than left as
cosmetic banner text pretending to be a real gate.

## The product shape this implies

A few things fall out of that structure that are worth naming explicitly,
because they're closer to product decisions than implementation details:

- **It's a digest, not a database.** You're never expected to search
  `cited.md` — you're expected to skim it once per session and make a small
  number of decisions. The dashboard exists for anything that needs to be
  queryable; the file exists to be *read*.
- **It's portable by construction.** Plain markdown, no app required, no
  login. Anyone can open it, copy a section into a note, paste it into a
  message to a friend ("here's where we should hike"). That portability is
  the actual argument for why this is a markdown file and not only a web
  page — the dashboard is one consumer of the underlying state, `cited.md`
  is another, and neither is privileged over the other in `plans.json`.
- **It's disposable and cheap to regenerate.** `publisher.render()` fully
  rebuilds the file from `plans.json` every pass — nothing in it is
  hand-maintained. That's a deliberate simplicity trade: no merge logic, no
  partial updates, no drift between what the file says and what the system
  actually believes. The cost is that a hand-edit to `cited.md` itself
  doesn't stick (see below).
- **Status is legible without reading prose.** The emoji scheme
  (⏳/✅/❌/🔀) exists so a person scanning ten plans can tell what still
  needs a decision without reading a single sentence — a small thing, but it's
  the difference between "report" and "inbox."
- **The feedback loop is one click away from the citation, not a separate
  workflow.** The "respond per item" line sits directly under the citations
  that justified the item being there — accept/reject is presented as a
  judgment on the evidence just shown, not a detached action somewhere else.

## What happens after you act on it

This is the "self-evolving" half, and it's why `cited.md` isn't a one-shot
report:

- **Accept** → the item's status updates, it gets tagged, and it becomes part
  of what VectorAI DB recall cites the *next* time something similar shows up.
- **Reject** → the item is pulled out of its plan and run back through the
  agent: tagged, checked against every *other* existing category for a better
  fit (`nearest_anchor_many`), and — if nothing fits — used as evidence toward
  promoting a genuinely new category via the taxonomy evolver, grounded in
  Senso and named by the local model. A rejection isn't a dead end; it's the
  input that makes the next `cited.md` categorize things better than this one
  did.
- **Enough rejections in a category** → the policy layer can suppress that
  category outright in future passes (high-confidence suppression), so
  `cited.md` gets *shorter* and more relevant over time rather than growing
  linearly with however much you've saved.

So the file you're looking at right now is provably worse than the one
you'll get next pass, in a way you can point to: fewer low-signal posts
filtered in ("low-signal post(s) filtered out" trending up), plans that match
categories you've actually confirmed, fewer repeats of things you've already
rejected.

## Known limitation, and what changed today

`cited.md` is a rendered view of `plans.json`, not a second source of truth —
so a hand-edit made directly to the file, between one pass and the next, has
always been overwritten on the next render. That's still true; making it
otherwise would mean either treating the file as an input the loop reads back
from (duplicating what `plans.json` + the feedback endpoints already do more
precisely) or skipping regeneration on some passes (leaving the published file
stale, which is worse).

What changed: that overwrite used to be silent. `publisher.render()` now
hashes the file's content right before every rewrite and compares it to the
hash it recorded after the *previous* render. A mismatch means a person (or
something) touched the file outside the pipeline since then, and that now
gets logged as a real session-log event (`cited_md_hand_edit_overwritten`) with the
edited content's own hash — so it's visible in the audit trail, and
recoverable, since `store.snapshot()` already backs up `cited.md` before every
pass that would overwrite it. The loss stopped being silent; it didn't stop
being a loss, because letting hand-edits survive would mean the file could
say something `plans.json` disagrees with, and that's the one guarantee
`cited.md` can't give up without giving up the "everything here is cited and
true right now" premise the whole file is built on.
