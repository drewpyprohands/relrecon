# Filter Ops Reference

Filters in recipes use a simple DSL that translates to Polars expressions. There are two categories of ops, two scopes where filters apply, and a clear extension path for each.

## Standard Ops

Simple column comparisons. Work in both **population filters** and **step filters** automatically.

| Op | Description | Value field | Example |
|---|---|---|---|
| `eq` | Exact match | `value` | `op: eq`, `value: "active"` |
| `neq` | Not equal | `value` | `op: neq`, `value: "inactive"` |
| `starts_with` | Prefix match | `value` | `op: starts_with`, `value: "V7"` |
| `not_starts_with` | Negated prefix | `value` | `op: not_starts_with`, `value: "V7"` |
| `contains` | Substring match | `value` | `op: contains`, `value: "Acme"` |
| `contains_any` | Any substring matches | `values` (array) | `op: contains_any`, `values: ["Data Migration", "Goblindor"]` |
| `is_not_null` | Field is not null | _(none)_ | `op: is_not_null` |
| `is_null` | Field is null | _(none)_ | `op: is_null` |

All standard ops cast the column to string before comparison, so they work on any column type.

The `is_not_null` and `is_null` ops are unary -- no `value` field needed. They skip the string cast and check null state directly. They combine naturally with other ops via the default `and` join:

```yaml
filter:
  - field: name
    op: is_not_null
  - field: status
    op: eq
    value: "active"
```

This keeps only rows where name IS NOT NULL **and** status = "active".

### Adding a new standard op

Add one `elif` block in `build_filter_expr()` in `src/recipe.py`:

```python
elif op == "ends_with":
    exprs.append(col.str.ends_with(cond["value"]))
```

Then add the op name to the enum in `config/recipe_schema.json` under both `definitions.filter_condition` and `definitions.step_filter_condition`.

That's it: the new op immediately works in population filters and step filters.

## Special Ops

Ops that require custom logic beyond a single Polars expression. These only work in **step filters** (not population filters).

| Op | Description | Value field | Why it's special |
|---|---|---|---|
| `max_age_years` | Date recency filter | `value` (number) | Tries 5 date formats to handle messy data, computes cutoff relative to today |

### Adding a new special op

Add handling in `_apply_step_filter()` in `src/matching.py`:

```python
if op == "max_age_years":
    return apply_date_gate(df, field, filt["value"])
elif op == "my_new_op":
    # Custom logic here
    return df.filter(...)
```

Then add the op name to the `step_filter_condition` enum in `config/recipe_schema.json` (but **not** to `filter_condition`: special ops don't work in population filters).

Special ops are for things that can't be expressed as a single Polars column expression: multi-format parsing, cross-column logic, computed values, etc.

## Scopes

### Population filters (`populations.*.filter`)

Applied during population building, **before any matching**. Only standard ops are supported.

```yaml
populations:
  pop1:
    source: multi_pop
    filter:
      - field: vendor_id
        op: starts_with
        value: "V7"
```

Multiple conditions default to `and` logic. Add `join: or` to switch. The `join` key can go on any condition dict in the list, or as its own standalone dict -- the first `join` found wins:

```yaml
# Option A: join on a condition
filter:
  - field: status
    op: eq
    value: "active"
  - field: status
    op: eq
    value: "pending"
    join: or

# Option B: join as standalone dict
filter:
  - field: status
    op: eq
    value: "active"
  - field: status
    op: eq
    value: "pending"
  - join: or
```

### Step filters (`steps.*.filters`)

Applied per matching step, **before name matching runs for that step**. Support standard ops + special ops. Have an `applies_to` field to control which side is filtered.

```yaml
steps:
  - name: Match Pop1 to core_parent
    source: pop1
    destination: core_parent
    filters:
      - field: Updated
        op: max_age_years
        value: 2
        applies_to: destination
      - field: status
        op: eq
        value: "active"
        applies_to: destination
    match_fields: [...]
```

`applies_to` options:
- `destination` (default): filter the destination DataFrame
- `source`: filter the source DataFrame
- `both`: filter both

### Legacy: `date_gate`

The `date_gate` step key is still supported but internally converts to a step filter with `op: max_age_years`. Prefer `filters` for new recipes:

```yaml
# Legacy (still works)
date_gate:
  field: Updated
  max_age_years: 2
  applies_to: destination

# Preferred
filters:
  - field: Updated
    op: max_age_years
    value: 2
    applies_to: destination
```

If both `date_gate` and `filters` exist in the same step, the date_gate is appended to the filters list.
