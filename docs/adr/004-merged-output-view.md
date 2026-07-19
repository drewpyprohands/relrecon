# ADR-004: Format List + Merged Matched/Unmatched View

**Status:** Accepted
**Date:** 2026-07-19
**Author:** AI Assistant
**Deciders:** drewpypro

---

## Context

`output.format` accepted only a single value, so emitting the same run as both
csv and parquet required two runs. Separately, reviewing match results meant
reconciling two artifacts by hand: the matched table and the unmatched
companion. Neither limitation involves the matching engine -- both are output
layer concerns. See [ADR-003](003-per-phase-output-model.md) for the per-phase
output model this extends.

## Decision

`output.format` accepts a list, so one run emits several formats. A single
string is unchanged.

`output.matched_unmatched` selects which matched/unmatched view(s) to emit
(single value or list):

```yaml
output:
  format: [csv, parquet]
  matched_unmatched: [merged, separate]
```

- **Key absent** -- legacy behavior exactly: separate matched artifact, plus
  an unmatched companion iff `emit_unmatched`. Merged requires explicit config.
- **`separate`** -- today's matched + unmatched artifacts (unchanged names).
- **`merged`** -- matched table with unmatched source rows appended, written as
  `{basename}_merged.{ext}` (csv/parquet) or folded into the xlsx report's
  Matched tab. Columns follow `output.columns.matched`; unmatched rows carry
  `match_step = unmatched` and empty values in derived columns. A final boolean
  `is_unmatched` column flags the origin. Merged is always emitted when
  configured, including with zero unmatched rows.

`is_unmatched` exists **only** in merged artifacts and is deliberately NOT
registered in `known_derived` -- it is not referenceable from `output.columns`.
The matching engine is unmodified; merged is a presentation-layer concat
(`build_merged_frame()` in `report.py`). Single-phase only. `emit_unmatched` is
deprecated: when `matched_unmatched` is present it is ignored (with a warning)
and `matched_unmatched` wins. `unmatched` is a reserved step name.

## Consequences

- Recipes without `matched_unmatched` are unaffected -- the legacy path is
  byte-for-byte unchanged, so this is a no-op for existing configs.
- A single-string `format` writes to the `--output` path verbatim; a list
  derives one path per format from the base, so listed formats never collide.
- Artifact count is formats x configured views, plus summary artifacts. The
  cross-product is intended and can grow quickly.
- `is_unmatched` is a documented exception to the `known_derived` registration
  convention, because the column exists only in merged artifacts.
- Deferred: multi-phase support, `match_mode: all_matches` with merged, the
  xlsx tab rename, and making `merged` the default -- each rejected by
  validation or documented unsupported rather than partially implemented.
