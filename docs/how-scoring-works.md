# How Scoring Works

The matching pipeline produces two scores per matched record: a **name score** (how the pair was found) and an **address score** (confidence signal or filter).
These are independent systems that run sequentially.

## Systems Overview

| System | Module | Config | Job |
|---|---|---|---|
| **Name matching** | matching.py | Recipe `match_fields` | Finds candidate pairs by comparing names |
| **Address scoring** | address.py | Recipe `address_support` + config files | Scores address similarity on matched pairs |

Address scoring involves three subsystems. Each does one job:

| System | Module | Config | Job |
|---|---|---|---|
| **Normalization** | normalize.py | `aliases.json`, `stopwords.json` | Text cleanup: lowercase, remove commas/periods/semicolons/colons (hyphens/apostrophes/ampersands preserved), expand aliases (blvd→boulevard), remove stopwords |
| **Parsing** | address.py | `address_patterns.json` (or libpostal) | Structural decomposition: extract street name, suffix, unit, state, zip |
| **Scoring** | address.py + RapidFuzz | -- | String similarity scoring on full address + street name, weighted combination |

These are stacked, not alternatives. Every address pair goes through all three.

**Alias scoping.** `aliases.json` may be scoped by field type:
`{"name": {...}, "address": {...}}`. Aliases under `name` apply to name match
fields; those under `address` apply to address scoring. An unscoped flat map
(e.g. `{"blvd": "boulevard"}`) applies to **addresses only** — this keeps
address abbreviations from bleeding into names — and the loader emits a warning.
To reach name fields, use the scoped form.

## Name Scoring

Name matching finds candidate pairs. Two methods:

**Exact** (`method: exact`): Polars inner join on normalized name values.
If the names are identical after tier normalization, they match.
Score is always **100**.

**Fuzzy** (`method: fuzzy`): RapidFuzz `cdist` computes a full score matrix (C++ backend, no Python loops).
Each source record gets the best-scoring destination above the threshold.
Score is **0-100** (e.g. 85.7 means 85.7% similarity).

Both methods try tiers in recipe order (e.g. `tiers: [raw, clean]`).
If a record matches on multiple tiers, the earlier tier in the list wins.

| Setting | Where | Default |
|---|---|---|
| Method | `match_fields.method` | `exact` |
| Tiers | `match_fields.tiers` | `[raw, clean]` |
| Threshold (fuzzy only) | `match_fields.threshold` | `80` |
| Scorer (fuzzy only) | `match_fields.scorer` | `token_sort_ratio` |

Available scorers: `token_sort_ratio`, `token_set_ratio`, `ratio`, `partial_ratio`, `WRatio`.

Name matching runs first. Address scoring runs only on records that passed name matching.

## Address Scoring

### Which Tools Use What

| Tool | normalize.py (clean/normalized) | address.py (libpostal/tokenizer) | RapidFuzz |
|---|---|---|---|
| Signal analysis | Yes -- clean tier for token analysis | No | No |
| Name matching | Yes -- tier depends on recipe `tiers` list | No | Only if `method: fuzzy` in recipe |
| Address scoring | Yes -- tiers from recipe `address_support.tiers` | Yes -- street name extraction | Yes -- always (full + street score) |

Note: "fuzzy" in name matching (RapidFuzz cdist on names) and "fuzzy" in address scoring (RapidFuzz token_sort_ratio on addresses) are unrelated. Name matching method (exact/fuzzy) does not affect address scoring -- address scoring runs the same way on every matched pair regardless of how the name match was found.

### Execution Order

For each address pair, per normalization tier:

```
1. NORMALIZE  apply_tier(address, tier, aliases, stopwords)
              raw: as-is | clean: lowercase + remove ,.;: | normalized: clean + aliases + stopwords
                    │
2. SCORE      rfuzz.token_sort_ratio(src_normalized, dst_normalized)
              produces full_score (0-100)
                    │
3. PARSE      parse_address(normalized_text, parser_mode)
              extracts street_name using libpostal or built-in tokenizer
                    │
4. STREET     rfuzz.ratio(street_src, street_dst)
              produces street_score (0-100), street_match = (street_score >= 80)
                    │
5. WEIGHT     both streets parsed? weighted = street*weight + full*(1-weight)
              can't parse street?  weighted = full_score
```

This runs for each tier (raw, clean, normalized) and each comparison pair.
Comparisons are generated dynamically from the configured address columns:
- **addrN<>addrM** for every source field N × destination field M (evaluated first)
- **merged<>merged** (all fields concatenated -- evaluated last)

With 2 fields per side: 5 comparisons (2×2 + merged). With 3 fields: 10 (3×3 + merged). With 4: 17.
The best weighted score across all tiers and comparisons wins. On equal scores,
the first comparison evaluated wins -- so specific field matches (addr1<>addr1)
are preferred over the noisier merged concatenation.

Scores use full float precision internally for all comparisons and dedup.
Rounding to 2 decimal places happens only in the report output layer.

**Why normalize before parse:** The parser sees cleaner input.
Alias expansion (blvd→boulevard) helps the built-in tokenizer match street suffixes.
Stopword removal reduces noise.
libpostal handles raw input fine, but clean input doesn't hurt it.

### Worked Example

Source: `"123 Main Blvd Suite 200"` vs Dest: `"123 MAIN BOULEVARD STE 200"`

Aliases: `{"blvd": "boulevard", "ste": "suite"}`  
Stopwords: `{"address": ["suite"]}`

#### RAW tier

```
normalize:  "123 Main Blvd Suite 200"  vs  "123 MAIN BOULEVARD STE 200"
full_score: token_sort_ratio → ~72 (case differs, blvd != boulevard)
parse:      street_name: "123 main"    vs  street_name: "123 main"
street:     ratio → 100, street_match = true
weighted:   100 * 0.6 + 72 * 0.4 = 88.8
```

#### CLEAN tier

```
normalize:  "123 main blvd suite 200"  vs  "123 main boulevard ste 200"
full_score: token_sort_ratio → ~82 (blvd != boulevard, suite != ste)
parse:      street_name: "123 main"    vs  street_name: "123 main"
street:     ratio → 100, street_match = true
weighted:   100 * 0.6 + 82 * 0.4 = 92.8
```

#### NORMALIZED tier (with aliases + stopwords)

```
normalize:  "123 main boulevard 200"   vs  "123 main boulevard 200"
            (blvd→boulevard, ste→suite, then "suite" removed as stopword)
full_score: token_sort_ratio → 100 (identical)
parse:      street_name: "123 main"    vs  street_name: "123 main"
street:     ratio → 100, street_match = true
weighted:   100 * 0.6 + 100 * 0.4 = 100.0
```

**Result:** normalized tier wins with score 100.0, comparison addr1<>addr1, street_match true.

### Street Name Weighting

When both the source and destination addresses have a parseable street name, the score is a
weighted blend of street similarity and full string similarity:

```
weighted = (street_score * street_weight) + (full_score * (1 - street_weight))
```

Default `street_weight` is `0.6` (60% street + 40% full string). This means:

- **Same street, different unit:** street_score is high, full_score is high -- score stays high
- **Different street, same city/state:** street_score is low, pulling the weighted score **down**
- **Unparseable street:** falls back to 100% full string score (no penalty or boost)

The `street_match` column in the report indicates whether street_score >= 80. By default this is
informational only -- it does not gate the match. But see **Street Match Gate** below.

You can tune the weight in your recipe:

```yaml
address_support:
  weights:
    street_name: 0.7  # more aggressive street emphasis
```

| street_name weight | Same street (street=100, full=75) | Different street (street=33, full=75) |
|---|---|---|
| 0.4 | 90.0 | 58.3 |
| 0.5 | 87.5 | 54.2 |
| 0.6 (default) | 85.0 | 50.0 |
| 0.7 | 82.5 | 45.8 |

Higher weight = more separation between same-street and different-street pairs.

### Street Match Gate (require_street_match)

For workflows where a different street name should **always** disqualify the match, enable the
street match gate:

```yaml
address_support:
  threshold: 75
  require_street_match: true   # reject when street names differ
```

When `require_street_match: true`:

- Records where `street_match` is false are **rejected** before the threshold check
- Rejected records cascade to later steps (or appear in Analysis with reason_code `street_mismatch`)
- The `best_rejected_score` still populates for transparency
- Records where street names can't be parsed (no street extracted) **are** rejected -- `street_match` defaults to `False` when streets can't be parsed, and the gate checks `street_match`

The gate runs **before** the threshold filter, so both can apply:

1. Street gate rejects different-street pairs
2. Threshold rejects remaining pairs with scores below cutoff

Default is `false` (backward compatible -- weighting only, no hard gate).

### What's Configurable vs Hardcoded

| Setting | Where | Configurable? |
|---|---|---|
| Address fields (source/dest) | Recipe `address_support.source/destination` | Yes |
| Parser mode (auto/libpostal/default) | Recipe `address_support.parser` | Yes |
| Score threshold | Recipe `address_support.threshold` | Yes |
| Tiers tried | Recipe `address_support.tiers` | Yes -- default `[raw, clean, normalized]` |
| Street weight | Recipe `address_support.weights.street_name` | Yes -- default `0.6` (60% street + 40% full) |
| Street match gate | Recipe `address_support.require_street_match` | Yes -- default `false` |
| Street match threshold (>=80) | Hardcoded in `score_address_pair` | No |
| Comparisons tried | Dynamic from field count | No -- always merged + all N×M individual combos |

## Understanding the Report Columns

The report shows several tier-related columns that can be confusing because they come from **independent systems**:

| Column | What it tells you | Set by |
|---|---|---|
| `match_tier` | Which normalization made the names join (how the pair was found) | Name matching |
| `addr_score` | Best weighted score across all address tiers (street + full string blend) | Address scoring |
| `addr_tier` | Which tier produced that best score (informational only) | Address scoring |
| `addr_comparison` | Which field combo scored best -- addr1<>addr1, merged<>merged, etc. (informational only) | Address scoring |
| `addr_street_match` | Whether extracted street names are similar (street_score >= 80). Informational unless `require_street_match: true` (then it gates the match). True = streets match, False = streets differ or couldn't be parsed | Address scoring |

`match_tier` and `addr_tier` are independent and often different.
Example:

```
Source name: "ACME CORP"     Dest name: "Acme Corp"
Source addr: "123 Main St"   Dest addr: "123 Main St"

Name matching: raw fails (case differs), clean matches → match_tier: clean
Address scoring: raw scores 100 (identical strings) → addr_tier: raw

Report shows: match_tier=clean, addr_tier=raw
```

This is correct -- the name needed cleaning to match, but the addresses were already identical raw.

Note: the report shows **original values** (pre-normalization) alongside tier metadata.
The tier tells you what normalization was applied internally to find the match or produce the score.

## Address Threshold and Cascading

When `address_support.threshold` is set (e.g. 75), it acts as a **quality filter**, not a record deletion:

All steps run against **all** source records independently. The threshold doesn't remove records from later steps -- every step gets the full source population.

1. Record matches on name in Step 1 → address score = 45 → below threshold → rejected from Step 1

2. Step 2 also receives this record (along with every other source record) and tries matching against a different destination

3. If Step 2 produces addr_score >= 75 → record is kept from Step 2

4. At the end, dedup picks the best result per record (earliest step, then highest scores)

5. If no step produces a passing score → record appears in the Analysis tab with `reason_code: addr_below_threshold` and `best_rejected_score: 45`

No records are lost.
The threshold just says "this match isn't good enough" for that particular step.

## Name Tiers vs Address Tiers

Name and address tiers are independent systems with independent config (ADR-002, Option B):

- **Name tiers**: `match_fields.tiers` -- controls which tiers are tried for name matching.
  Position in list = priority (first wins ties).

- **Address tiers**: `address_support.tiers` -- controls which tiers are tried for address scoring.
  Default: `[raw, clean, normalized]` when omitted.
  Position in list determines tie-breaking (last tier wins at equal scores -- the code uses strict `>`, so a later tier with an identical score replaces an earlier one).

## Asymmetric Field Counts

Source and destination can have different numbers of address fields. With 3 source and 2 destination fields: 1 + 3×2 = 7 comparisons. The code generates all cross-combinations regardless of whether the field counts match.

In the report, `addr_comparison` reflects the actual fields compared -- you may see labels like `addr3<>addr1` when the source has more fields than the destination (or vice versa). The numbers correspond to position in the `address_support.source` and `address_support.destination` lists.

## Reason Codes

Unmatched records in the Analysis tab get a `reason_code` explaining why they didn't make it to the Matched tab:

| Code | Meaning |
|---|---|
| `no_name_match` | No name match was found in any step -- the record never passed the name matching phase |
| `addr_below_threshold` | Name matched in at least one step but the address score was below threshold in every step |
| `street_mismatch` | Name matched but the street match gate rejected it (only when `require_street_match: true`) |

The `rejection_step` column shows which step came closest and `best_rejected_score` shows the highest address score achieved before rejection.

## record_key and Dedup

The `record_key` field in a population config identifies the unique record identifier (e.g. `vendor_id`). It's critical for correct dedup behavior in `best_match` mode -- the pipeline deduplicates on this key to keep one result per source record.

Without `record_key`, the pipeline falls back to the match field (e.g. `l3_fmly_nm`), which can collapse records that share the same name into a single result. A runtime warning is emitted when `record_key` is missing.

## Reserved Column Names

The join engine uses the `_dst` suffix to disambiguate destination columns when source and destination share column names (e.g. both populations having `hq_addr1`).
Source data column names should not end in `_dst` to avoid conflicts with this internal naming.

## Tie-Breaker (output.tie_breaker)

When a source record matches multiple destination records with identical names (exact match) or identical fuzzy scores, the tie-breaker selects the winner by sorting on a destination column.

**Important:** The tie-breaker is a secondary sort. Name score and address score always take priority. The tie-breaker only decides between matches that are otherwise equal.

### Configuration

```yaml
output:
  tie_breaker:
    column: supplier_id        # destination column to sort by
    strip_prefix: alpha        # optional: how to process values before sorting
    order: asc                 # asc (lowest wins, default) or desc (highest wins)
```

### strip_prefix modes

- **Omitted or empty:** Sort by raw string value.
- **`alpha`:** Strip all leading letters and parse the remainder as an integer. Useful for IDs like `S1013` -- strips `S`, sorts by `1013` numerically. Values that can't be parsed as integers sort last.
- **Any other string:** Treated as a literal prefix to strip from the start of the value before sorting as text. For example, `strip_prefix: "ID-"` converts `"ID-500"` to `"500"` but leaves `"OTHER-500"` unchanged.

### How it works

1. Before matching, the destination DataFrame is pre-sorted by the tie-breaker column
2. The join preserves destination ordering (`maintain_order="right"`)
3. Per-step dedup (`unique(keep="first")`) picks the first match -- which is the tie-breaker winner
4. Cross-step dedup includes the tie-breaker as a secondary sort key (safety net)

### Interaction with fuzzy matching

For fuzzy matching, the tie-breaker only matters when multiple destination records produce the same fuzzy score. When scores differ, the highest score always wins regardless of tie-breaker preference. This is correct behavior -- match quality should take priority over arbitrary column preferences.

## Final Rollup (output.final_rollup)

`tie_breaker` resolves ties **within a single matching step**. Records of the same real-world family that match in **different** steps (one via exact L3, another via exact L1) each keep the value of their own step -- the tie-breaker cannot unify them. `final_rollup` is a **terminal, non-destructive** pass that runs once after all matching and resolution, rolling a configurable group to its lowest (or highest) target value.

| | `tie_breaker` | `final_rollup` |
|---|---|---|
| When | during matching (per step) | after resolution (terminal) |
| Scope | one step's duplicate matches | a group across chosen steps |
| Effect | selects the surviving match | adds columns; originals untouched |
| Recipes | single-phase (multi-phase gap, tracked separately) | single-phase only |

### Configuration

```yaml
output:
  final_rollup:
    - steps: [Exact Name Match L3, Exact Name Match L1]  # optional; omit for all steps
      group_key: derived_supplier_nm    # matched-output column to group by
      group_key_tier: normalized        # raw | clean | normalized (default raw)
      target: derived_supplier_id       # column minimized within the group
      strip_prefix: alpha               # same semantics as tie_breaker
      order: asc                         # asc (lowest wins, default) | desc
      write_to: rolled_supplier_id       # additive output column (default)
```

### Semantics

- Rows are filtered to `match_step in steps` (all steps when `steps` is omitted), grouped by `group_key` (after the `group_key_tier` transform), and every member is assigned the tie-broken min of `target` (reusing `strip_prefix`/`order`).
- The result is written to `write_to`; rows outside `steps` keep their own `target` value there. A boolean `<write_to>_changed` flag is set where `write_to` differs from the row's own `target`.
- Implemented as a group aggregation (not self-population matching), so a group's minimum-holder is included by construction and retains its own id.
- Grouping is exact equality after the tier transform -- entity variance belongs in matching steps, not here. Multiple buckets are allowed but must use distinct `write_to` columns.

### Validation

`final_rollup` is rejected in multi-phase recipes, on unknown `steps` names, on `group_key`/`target` columns absent from the matched output, on invalid `group_key_tier`/`order` enums, on two buckets sharing a `write_to`, and on a `write_to` that collides with an existing source/derived/target column (which would overwrite it and force `<write_to>_changed` always-false). `steps` is optional -- omit it to apply the bucket to all steps.

## Same-Population Matching

When a step's `source` and `destination` refer to the same population, the pipeline automatically enables self-exclusion: records won't match against themselves.

### How it works

- **Auto-detection:** `if src_pop == dst_pop` in the step config triggers same-pop mode
- **Self-exclusion key:** Uses the population's `record_key` to identify self-matches
- **Exact matching:** After the join, rows where `record_key == record_key_dst` are filtered out
- **Fuzzy matching:** The score matrix diagonal (self-match cells) is zeroed before selecting best matches

### Example

```yaml
populations:
  all_vendors:
    source: vendor_data
    record_key: vendor_id

steps:
  - name: Find Duplicate Vendors
    source: all_vendors
    destination: all_vendors      # same population -- self-exclusion auto-enabled
    match_fields:
      - source: company_name
        destination: company_name
        method: fuzzy
        threshold: 75
```

Records with unique names will appear in the Unmatched tab. Records with similar names will be paired with their closest match (excluding themselves).

### Without record_key

If the population has no `record_key`, the pipeline falls back to the match field for self-exclusion. For exact matching this means all matches are filtered out (same name = same key = self-match). For fuzzy matching, only records with identical names are excluded while similar-but-different names still match. A runtime warning is emitted recommending you add `record_key`.