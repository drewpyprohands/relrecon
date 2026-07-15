# Cross-Step Rollup Design Note

Investigation behind issue **#67 (I4. Final tie-breaker step)**. Captures why the
existing per-step tie-breaker cannot roll a family to a single sfam across steps,
which approaches were rejected and why, and the chosen design. Fixtures on branch
`spike/cross-step-tiebreaker` back every claim.

---

## 1. Problem statement

The per-step tie-breaker (`output.tie_breaker`) only resolves ties **within a
single matching step**. When records that belong to the same real-world family
match in **different steps** (e.g. one via exact L3, another via exact L1), each
keeps the sfam of its own step. There is no mechanism to roll the whole family to
the lowest sfam.

Root cause is the resolution sort in `_resolve_matches`
(`src/matching.py:802-824`):

```text
sort by [_step_order, name_score, addr_score, _tb_sort_key]  →  unique(source_key, keep=first)
```

`_step_order` is the **first** key; `_tb_sort_key` (the tie-breaker) is the
**last**. So an earlier step always wins, and the tie-breaker only decides
between candidates that are otherwise equal — effectively within one step. Across
steps it is inert for family rollup.

---

## 2. Evidence

Fixtures: `data/crossstep_source.csv`, `data/crossstep_dest.csv`. The "Company
XYZ Holdings" family is reachable at different sfams depending on the step; the
orphan `V012` (parent matches no `supplier_nm`) can only reach a sfam via L3.

| record | matched at (default) | default sfam | L1-first reorder | desired (family min) |
|--------|----------------------|--------------|------------------|----------------------|
| V001   | Exact L3             | S1013        | S0500            | S0500 |
| V002   | Exact L1             | S0500        | S0500            | S0500 |
| V003   | Exact L1             | S0500        | S0500            | S0500 |
| V010   | Exact L3 (Zeta)      | S0200        | S0100            | S0100 |
| V011   | Exact L3 (Zeta)      | S0100        | S0100            | S0100 |
| V012   | Exact L3 (orphan)    | **S0200**    | **S0200**        | S0100 |

`V012` diverges under **every** step ordering: its parent matches no
`supplier_nm`, so it only reaches a sfam through the L3 step and never sees the
family minimum. This proves no ordering is a general fix.

---

## 3. Rejected approaches

### 3.1 Step reordering (L1 before L3)
Unifies most of a family, but:
- Forces L1 (lower confidence) to outrank L3 for **every** record — a semantics
  change, not a rollup.
- Still fails `V012` (see table) — the orphan never reaches the family minimum.

### 3.2 Two-population self-match (`_previous_matched` → self-pop rollup)
The idea: split the matched output into an L3 population and an L1 population by
`match_step`, then self-match each and inherit the lowest sfam. Blocked on three
independent findings:

1. **Filters ignored on `_previous_matched`.** `_build_populations`
   (`src/matching.py:668-670`) returns early for `_previous_matched` and the
   phase runner assigns the whole prior set; the `filter:` is never applied.
   Probe: both `pop_l3` and `pop_l1` came out at height 10 (the full set). The
   filter shape itself is valid — the same filter on a normal source column
   returns the right rows; the gap is `_previous_matched`-specific.
2. **Multi-phase has no tie-breaker.** See §4.
3. **Self-exclusion mis-anchors the group minimum.** Same-population matching
   excludes a record from matching its own row, so the family's lowest-sfam
   record cannot match itself — it inherits its next-lowest sibling and is pushed
   **up**. Fixture `data/selfpop_anchor.csv` + `config/recipes/selfpop_anchor.yaml`
   (single phase, real tie-breaker):

   | record | own sfam | rolled sfam |
   |--------|----------|-------------|
   | A (G1) | S300     | S100 |
   | B (G1) | **S100** | **S200** ← minimum-holder pushed up |
   | C (G1) | S200     | S100 |

   A `least(own, rolled)` patch recovers all three to S100 — but that is a
   band-aid for the self-pop path. A **group aggregation** includes the
   minimum-holder by construction and never needs it.

---

## 4. Multi-phase gaps found

Both are silent-failure bugs independent of whether the rollup ever ships.

- **`tie_breaker` / `match_mode` are unread in multi-phase.** They are read only
  at `src/matching.py:1057-1058` (single-phase) and `1105-1106` (multi-phase),
  both from `recipe.get("output", ...)` — i.e. **top-level** output. The
  validator forbids a top-level `output` on multi-phase recipes
  (`src/recipe.py:175`; documented in `docs/multi-phase-recipes.md:130`), and
  nothing reads these keys from a per-phase output block (per-phase handling
  covers `format`/`summary`/`columns` only — `src/recipe.py:200,261`). Net: in
  any multi-phase recipe `tie_breaker` is `None` and `match_mode` defaults to
  `best_match`, always. The phased spike confirmed it — the Company XYZ family
  rolled to S2456 (highest) with no tie-break pre-sort.
- **`_previous_matched` ignores population filters** (`src/matching.py:668-670`).
  Dry-run claims to validate that "population filters reference valid fields"
  (`docs/multi-phase-recipes.md:182`), so validator and runtime disagree. It
  should either apply the filter or hard-error, never silently return the whole
  set.

---

## 5. Chosen design

A **terminal group-aggregation pass** over the resolved matched set
(`group_by(key) → tie-broken min of the target sfam`), not self-pop:

- **Group aggregation, not self-pop** — the minimum-holder is in its own group,
  so §3.2.3 cannot occur.
- **Non-destructive** — keep the original sfam; write the rolled value to a
  configurable column (default `rolled_supplier_id`) and set `rollup_changed`
  for reviewers.
- **Per-bucket config** — scope (which steps/level), group key (default: source
  tier-name, e.g. `l3_fmly_nm`, to preserve the confidence boundary), target
  column, and `strip_prefix`/`order` (reuse existing tie-breaker semantics).
- **v1 scope** — single-phase only (the file-based recipe-2-over-recipe-1 flow).
  Inline multi-phase rollup is a follow-up contingent on §4. Address-score gating
  is deferred (it requires re-scoring against the *rolled* destination).

Full spec and acceptance criteria live in issue **#67**. Two constraints belong
there so they cannot resurface: *implement as group aggregation, NOT self-pop*,
and an acceptance test that *a group's minimum-holder retains its own ID after
rollup* (the A/B/C→S100 fixture below).

---

## 6. Fixture inventory

| file | exercises |
|------|-----------|
| `data/crossstep_source.csv` / `data/crossstep_dest.csv` | Cross-step family divergence, incl. the orphan `V012` that defeats every step ordering (§2) |
| `config/recipes/crossstep_tiebreaker.yaml` | Working single-phase spike: L3/L1 exact + fuzzy steps with `output.tie_breaker`; reproduces default vs desired sfam |
| `data/selfpop_anchor.csv` / `config/recipes/selfpop_anchor.yaml` | Self-pop anchor bug in isolation; doubles as the #67 acceptance test (minimum-holder must retain its own ID → all of A/B/C resolve to S100 under a correct group-min) |
