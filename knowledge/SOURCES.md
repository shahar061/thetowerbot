# Knowledge pack sources

This file records where the wiki corpus behind the knowledge pack came from,
how to re-extract it, and which source conflicts could not be attached to a
specific fact id (and so are not represented as `conflicts[]` entries in the
pack itself).

## Provenance

The corpus is a crawl of the Tower Hub wiki, captured on a research branch
that has not been merged. It is not vendored into this repository — only its
digests are, following the precedent set by `catalog/concepts.v1.json`, whose
`sources[]` entries record file names and sha256 digests for files that are
deliberately absent from the repo.

- Branch: `codex/autonomy-research`
- Commit: `6b4e1b4` ("Record wiki source review completion metrics")
- Path on that branch: `docs/research/wiki-source-2026-09-05/`
- Retrieval date recorded in the corpus manifest: `2026-09-05`
- Page count recorded in the corpus manifest: `247` (the `sources[]` array in
  `manifest.json`)
- File count of the full extracted corpus directory (pages, indices, and
  crawl tooling together): `658`

Do not check out `codex/autonomy-research` to get this corpus — that
checkout is intentionally left dirty and must not be modified. Extract with
`git archive` instead, as below.

## Re-extraction recipe

Run from the repository root:

```sh
mkdir -p research
git fetch origin 'refs/heads/codex/autonomy-research:refs/remotes/origin/codex/autonomy-research'
git archive origin/codex/autonomy-research docs/research/wiki-source-2026-09-05 \
  | tar -x --strip-components=2 -C research
git show origin/codex/autonomy-research:docs/research/2026-09-05-wiki-audit.md \
  > research/wiki-audit.md
git show origin/codex/autonomy-research:docs/research/2026-09-05-wiki-supplement.md \
  > research/wiki-supplement.md
```

This populates `research/wiki-source-2026-09-05/` (the page corpus, gitignored
under `/research/`) plus the two review documents that motivated this file.

## Digests the pack cites

The pack's `sources[]` entries cite these whole-corpus digests, and — for any
fact traceable to a single page — that page's own `sha256` from
`manifest.json`'s per-page `sources[]` array instead. Prefer the per-page
digest: a fact whose source page changed should be invalidated without
invalidating every fact drawn from an unrelated neighbour page.

| File | sha256 | bytes |
|---|---|---|
| `manifest.json` | `6e3e20829dedc92dd2b04564f3b00cda871267c19d0b317dce879694b1f93c5a` | 461539 |
| `concept-inventory.json` | `518fa739673e34f9c3cbf8b78e8a1949d06049b385c811c3497eb04bce2d24cb` | 124722 |
| `glossary-terms.json` | `8e8ae5805a58d78b9ad786ead96bde92f1306bd4b5f589c38fd5ad69e6f4d952` | 30500 |
| `domains.json` | `2d0f376a61d44de54ec0126bb961abd507ae4a58cfdcecb7ae3be4fca89af936` | 24720 |

The `concept-inventory.json` and `glossary-terms.json` digests above match
the `wiki-inventory` and `wiki-glossary` entries already recorded in
`catalog/concepts.v1.json` — same files, same capture.

## Source conflicts that could not be attached to a fact id

`research/wiki-supplement.md`'s "Source conflict register" table and
`research/wiki-audit.md`'s "Important source limitations" prose were
reviewed in full to build the pack's `conflicts[]` list. Most named
conflicts point at a specific fact (a cap, a cost, a category, an effect
direction) and are recorded there instead of here. A few describe general
unreliability of a page or a guide rather than a contradiction between two
identifiable claims, so they don't taint any single fact id. They're kept
here as caveats for anyone extending the pack:

- The enemy overview mispairs several headings with the wrong enemy name and
  lab content — a structural extraction hazard, not a value contradiction.
- The Shards currency page is an explicit stub; its module-shard and
  reroll-shard distinction should be sourced from the module pages instead.
- The Vault system (three trees, extra presets, sliders, life-saving
  ordering) is more feature-complete than older pages assume — a staleness
  gap rather than a contradiction with a specific figure.
- The Gameplay overview's enemy-type count is likely stale in the same way
  its currency count is; no dedicated page was reviewed to give a
  contradicting figure to pin against it.
- The Devo guide explicitly warns it predates a Death Wave rework, and
  IceTæ's gem/card priority list is dated to an old version range. Both are
  self-flagged as stale rather than contradicted by a second source.
- Advanced Analysis is a content placeholder; the 100% AFK Orb Devo and 50s
  SMax guides describe external strategies without embedding them. Reading
  the Hub does not substitute for reading those external documents.
- The Tier-Specific Guide and the Orbless guide describe conditional,
  branch-specific strategies that cannot be merged into one priority list —
  a scope caveat, not a factual contradiction.
- Sub-effect roll-pool legality ("effect rarity is bounded by module
  rarity... and vice versa") is stated ambiguously rather than
  contradicted by a second claim; the rule itself needs a tested
  definition before it can be treated as a fact.
- Cross-page system-identity mislabeling (Game Speed and Starting Cash
  called Workshop upgrades while Labs are described elsewhere; the Rend
  enhancement called a lab; "during a run" spending wording) spans several
  concepts' category identity rather than a single fact's value, so it is
  recorded here rather than split speculatively across fact ids.

### Conflicts with a clear tainted claim, but no fact in this pack to taint

The conflicts above either describe general unreliability with no second
claim to contradict, or a single-page internal ambiguity. The conflicts
below are different in kind: each names a genuine two-source contradiction
over a specific value, but that value belongs to a mechanic this pack's
current fact families (tier unlock gates, milestone rewards, lab/UW/gem/card
priority ordering, and the footgun-style hazards) do not hold a fact about.
A conflict with no tainted fact id is not a conflict this pack can act on —
it is recorded here so that a later pack extending into these mechanics can
see immediately which conflicts become live the moment the corresponding
fact is added, rather than re-deriving the register from scratch:

- The enemy overview's Protector spawn cap (given as both 8 and 10) has no
  enemy-mechanic fact in this pack to taint.
- The Free Upgrades combined cap (90.75% stated alongside overflow above
  100%) has no free-upgrade fact in this pack.
- Fleet spawn scope (Tier-14-exclusive wording versus documented high-wave
  fleets on every tier) has no fleet/enemy fact in this pack.
- The Modules overview's substat slot thresholds (only through 161 in one
  place, 201 and 241 elsewhere) have no module fact in this pack.
- The currency overview's claimed count of six currencies has no
  currency-count fact in this pack.
- Critical Coin's trigger predicate (critical shots only, per the card page,
  versus all qualifying kills except noncritical bullets, per the newer
  Workshop article) has no Critical Coin fact in this pack — the `coins`
  card this pack's `priority.cards.unlock_order` names is a different card
  entirely (`cards.coins`, not `cards.critical-coin`).
- The Wave Accelerator/Energy Shield charge-accumulation clock disagreement
  (boss-spacing versus game-time) has no charge-timing fact in this pack.
- The Critical/Super Critical enhancement's increments (inconsistent with
  its own stated level cap and final multiplier) have no enhancement fact in
  this pack.
- The Attack Speed page's display-precision caveat (behavior still under
  analysis; displayed speed is not shots/second) is about the Workshop
  Attack Speed mechanic, not the `attack_speed` card this pack's
  `priority.cards.unlock_order` names — no fact about the underlying
  display formula exists in this pack to taint.
- The within-page-disagreements bundle (Chips' universal-nearest-then-
  Attack-targeting wording; Bot Bot's prose bonus increment versus its own
  table; the Dissonant Runs Guide's starting-tier wording and missing
  package exception) has no Chips, Bot Bot, or Dissonant-Runs fact in this
  pack.
- Six of the seven glossary-vs-dedicated-page mismatches have no fact in
  this pack: Galaxy Compressor's module type, Fortress's base effect,
  Plasma Cannon's target, Demon Mode's cost/benefit framing, Death Ray's
  boss/elite eligibility, and the four-versus-five bot count. The seventh —
  Enemy Balance's spawn direction — does taint a fact here
  (`priority.cards.unlock_order`) and is recorded as a live `conflicts[]`
  entry, not in this list.
