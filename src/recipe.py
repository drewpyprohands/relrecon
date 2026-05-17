"""
Recipe config loader and validator.

Loads YAML/JSON recipes, validates structure, resolves data sources,
and parses the filter DSL into Polars expressions.
"""

import json
from pathlib import Path

import polars as pl

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


def load_recipe(path: str) -> dict:
    """Load a recipe from YAML or JSON file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Recipe not found: {path}")

    with open(p) as f:
        if p.suffix in (".yaml", ".yml"):
            if not YAML_AVAILABLE:
                raise ImportError("PyYAML required for YAML recipes: pip install pyyaml")
            try:
                recipe = yaml.safe_load(f)
            except yaml.YAMLError as exc:
                # Extract line/column from PyYAML's error marks
                msg = f"YAML syntax error in {path}"
                if hasattr(exc, "problem_mark") and exc.problem_mark:
                    mark = exc.problem_mark
                    msg += f" (line {mark.line + 1}, column {mark.column + 1})"
                if hasattr(exc, "problem") and exc.problem:
                    msg += f": {exc.problem}"
                msg += (
                    "\n\nThis is usually caused by a tab character or "
                    "incorrect indentation. YAML requires spaces, not tabs."
                )
                raise ValueError(msg) from None
        else:
            recipe = json.load(f)

    recipe = _apply_step_defaults(recipe)
    schema_warnings = validate_recipe(recipe)
    if schema_warnings:
        import sys
        for w in schema_warnings:
            print(f"[WARN] {w}", file=sys.stderr)
    return recipe


# ---------------------------------------------------------------------------
# Step defaults
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge override into base. Override wins on conflicts."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _apply_step_defaults(recipe: dict) -> dict:
    """Merge step_defaults into each step (step values win on conflict).

    Removes step_defaults from the recipe after applying so downstream
    validation only sees fully-expanded steps.
    """
    defaults = recipe.pop("step_defaults", None)
    if not defaults:
        return recipe
    for i, step in enumerate(recipe.get("steps", [])):
        recipe["steps"][i] = _deep_merge(defaults, step)
    return recipe


# ---------------------------------------------------------------------------
# JSON Schema validation
# ---------------------------------------------------------------------------

def _load_recipe_schema() -> dict:
    """Load the recipe JSON Schema from config/recipe_schema.json."""
    schema_path = Path(__file__).resolve().parent.parent / "config" / "recipe_schema.json"
    if not schema_path.exists():
        return {}  # Gracefully degrade if schema file missing
    with open(schema_path) as f:
        return json.load(f)


def validate_recipe(recipe: dict) -> list[str]:
    """Validate recipe against JSON Schema + additional semantic checks.

    Raises ValueError for critical structural errors (missing required
    fields, wrong types).
    Returns a list of non-fatal warnings (unrecognized keys, missing
    record_key, etc.).
    """
    warnings: list[str] = []

    schema = _load_recipe_schema()
    if schema:
        try:
            from jsonschema import Draft7Validator
        except ImportError:
            warnings.append(
                "jsonschema not installed -- schema validation skipped. "
                "Install with: pip install jsonschema"
            )
        else:
            validator = Draft7Validator(schema)
            errors = sorted(validator.iter_errors(recipe), key=lambda e: list(e.path))
            schema_errors = []
            for err in errors:
                path = err.json_path or "$"
                # additionalProperties violations are warnings (typos, indent bugs)
                if err.validator == "additionalProperties":
                    warnings.append(f"{path}: {err.message}")
                else:
                    schema_errors.append(f"{path}: {err.message}")
            if schema_errors:
                detail = "\n  ".join(schema_errors)
                raise ValueError(f"Recipe schema validation failed:\n  {detail}")

    # Collect all critical errors, raise once at the end
    critical: list[str] = []

    # Fallback for environments without jsonschema
    required = ["name", "sources", "populations", "steps", "output"]
    missing = [k for k in required if k not in recipe]
    if missing:
        critical.append(f"Recipe missing required fields: {missing}")

    for name, src in recipe.get("sources", {}).items():
        if "file" not in src and "loader" not in src:
            critical.append(f"Source '{name}' missing 'file' or 'loader' field")

    step_names_seen: dict[str, int] = {}
    for i, step in enumerate(recipe.get("steps", [])):
        for k in ["name", "source", "destination", "match_fields"]:
            if k not in step:
                critical.append(f"Step {i} ('{step.get('name', '?')}') missing '{k}'")
        sname = step.get("name")
        if sname is not None:
            if sname in step_names_seen:
                critical.append(
                    f'Duplicate step name "{sname}" (steps {step_names_seen[sname]} and {i}). '
                    'Each step must have a unique name.'
                )
            step_names_seen[sname] = i

    if "output" in recipe and "format" not in recipe["output"]:
        critical.append("Output missing 'format' field")

    source_pops = {step["source"] for step in recipe.get("steps", []) if "source" in step}
    dest_pops = {step["destination"] for step in recipe.get("steps", []) if "destination" in step}
    for pop_name in source_pops:
        pop_cfg = recipe.get("populations", {}).get(pop_name, {})
        if pop_cfg.get("action") == "exclude":
            critical.append(
                f'Population "{pop_name}" has action: exclude but is used as a '
                f'step source. Remove action: exclude -- it is only for garbage '
                f'populations that should be subtracted from remainders.'
            )
        if "record_key" not in pop_cfg:
            warnings.append(
                f'Population "{pop_name}" has no record_key. '
                "Dedup will fall back to match field -- records with "
                "duplicate names may be collapsed. Set record_key to the "
                "field that uniquely identifies each source record."
            )

    for pop_name in dest_pops:
        pop_cfg = recipe.get("populations", {}).get(pop_name, {})
        if pop_cfg.get("action") == "exclude":
            warnings.append(
                f'Population "{pop_name}" has action: exclude but is used as a '
                f'step destination. This may produce unexpected results.'
            )

    if critical:
        if len(critical) == 1:
            raise ValueError(critical[0])
        detail = "\n  - ".join(critical)
        raise ValueError(f"Recipe validation failed ({len(critical)} errors):\n  - {detail}")

    return warnings


def load_source(source_config: dict, base_dir: str = ".",
                recipe_name: str = "", source_name: str = "") -> pl.DataFrame:
    from loaders import dispatch_loader
    return dispatch_loader(
        source_config, base_dir,
        recipe_name=recipe_name, source_name=source_name,
    )


def build_filter_expr(filter_config: list, join_mode: str = "and") -> pl.Expr:
    """Build a Polars expression from the filter DSL.

    Each condition: {field, op, value/values}
    join_mode is set at filter list level (not per-condition).
    """
    exprs = []

    # Extract join_mode from any condition that declares it (legacy support)
    for cond in filter_config:
        if "join" in cond:
            join_mode = cond["join"]
            break

    for cond in filter_config:

        field = cond["field"]
        op = cond["op"]
        ignore_case = cond.get("ignore_case", False)
        col = pl.col(field).cast(pl.String)

        # When ignore_case is set, lowercase the column for comparison
        if ignore_case:
            col = col.str.to_lowercase()

        if op == "eq":
            value = cond["value"].lower() if ignore_case else cond["value"]
            exprs.append(col == value)
        elif op == "neq":
            value = cond["value"].lower() if ignore_case else cond["value"]
            exprs.append(col != value)
        elif op == "starts_with":
            value = cond["value"].lower() if ignore_case else cond["value"]
            exprs.append(col.str.starts_with(value))
        elif op == "not_starts_with":
            value = cond["value"].lower() if ignore_case else cond["value"]
            exprs.append(~col.str.starts_with(value))
        elif op == "contains":
            value = cond["value"].lower() if ignore_case else cond["value"]
            exprs.append(col.str.contains(value, literal=True))
        elif op == "contains_any":
            values = [v.lower() for v in cond["values"]] if ignore_case else cond["values"]
            sub = col.str.contains(values[0], literal=True)
            for v in values[1:]:
                sub = sub | col.str.contains(v, literal=True)
            exprs.append(sub)
        elif op == "is_not_null":
            exprs.append(pl.col(field).is_not_null())
        elif op == "is_null":
            exprs.append(pl.col(field).is_null())
        else:
            raise ValueError(f"Unknown filter op: {op}")

    if not exprs:
        return pl.lit(True)

    result = exprs[0]
    for e in exprs[1:]:
        result = result | e if join_mode == "or" else result & e
    return result


def filter_population(df: pl.DataFrame, pop_config: dict) -> pl.DataFrame:
    """Filter DataFrame by population config."""
    if "filter" not in pop_config or not pop_config["filter"]:
        return df
    return df.filter(build_filter_expr(pop_config["filter"]))


# ---------------------------------------------------------------------------
# Semantic field validation
# ---------------------------------------------------------------------------

class RecipeValidationError(Exception):
    """Raised when semantic validation finds critical field errors."""
    pass


def validate_fields(
    recipe: dict,
    sources: dict[str, pl.DataFrame],
    populations: dict[str, dict],
) -> tuple[list[str], list[str]]:
    """Validate all recipe field references against loaded DataFrames.

    Args:
        recipe: Parsed recipe dict
        sources: {name: DataFrame} from loaded CSV/Parquet files
        populations: {name: {config, df, source}} after filtering

    Returns:
        (errors, warnings). Errors are critical (match_fields, inherit),
        warnings are non-fatal (address_support, date_gate, filter).
    """
    errors = []
    warnings = []

    def _check(field: str, df: pl.DataFrame, context: str, critical: bool = True):
        if field not in df.columns:
            available = ", ".join(sorted(df.columns)[:10])
            msg = f'{context}: field "{field}" not found. Available: {available}'
            if critical:
                errors.append(msg)
            else:
                warnings.append(msg)

    # Validate population filter fields
    for pop_name, pop_cfg in recipe["populations"].items():
        src_name = pop_cfg.get("source", "")
        if src_name not in sources:
            continue  # Source loading would have already failed
        src_df = sources[src_name]
        for cond in pop_cfg.get("filter", []):
            if "field" in cond:
                _check(
                    cond["field"], src_df,
                    f'Population "{pop_name}" filter',
                    critical=False,
                )

    # Validate step field references
    for i, step in enumerate(recipe["steps"]):
        step_label = f'Step {i+1} "{step.get("name", "?")}"'
        src_pop = step.get("source", "")
        dst_pop = step.get("destination", "")

        src_df = populations.get(src_pop, {}).get("df")
        dst_df = populations.get(dst_pop, {}).get("df")

        # If destination is a source (not a population), check sources
        if dst_df is None and dst_pop in sources:
            dst_df = sources[dst_pop]

        # match_fields (critical)
        for mf in step.get("match_fields", []):
            if src_df is not None:
                _check(mf["source"], src_df, f"{step_label} match_fields.source", critical=True)
            if dst_df is not None:
                _check(mf["destination"], dst_df, f"{step_label} match_fields.destination", critical=True)

        # address_support (warning)
        if "address_support" in step:
            ac = step["address_support"]
            for af in ac.get("source", []):
                if src_df is not None:
                    _check(af, src_df, f"{step_label} address_support.source", critical=False)
            for af in ac.get("destination", []):
                if dst_df is not None:
                    _check(af, dst_df, f"{step_label} address_support.destination", critical=False)

        # date_gate (warning, legacy still supported)
        if "date_gate" in step:
            dg = step["date_gate"]
            dg_field = dg.get("field", "")
            applies_to = dg.get("applies_to", "destination")
            check_df = dst_df if applies_to == "destination" else src_df
            if check_df is not None and dg_field:
                _check(dg_field, check_df, f"{step_label} date_gate", critical=False)

        # step filters (warning)
        for fi, filt in enumerate(step.get("filters", [])):
            filt_field = filt.get("field", "")
            applies_to = filt.get("applies_to", "destination")
            if applies_to in ("destination", "both") and dst_df is not None and filt_field:
                _check(filt_field, dst_df, f"{step_label} filters[{fi}] (destination)", critical=False)
            if applies_to in ("source", "both") and src_df is not None and filt_field:
                _check(filt_field, src_df, f"{step_label} filters[{fi}] (source)", critical=False)

        # inherit (critical)
        for inh in step.get("inherit", []):
            if dst_df is not None:
                _check(
                    inh["source"], dst_df,
                    f"{step_label} inherit",
                    critical=True,
                )

    # Validate output.columns field references
    # These are explicitly requested by the recipe author, so missing
    # fields are errors (not warnings). The report will silently drop them.
    output_columns = recipe.get("output", {}).get("columns", {})
    # Collect all known source columns for validation
    all_source_cols: set[str] = set()
    for src_df in sources.values():
        all_source_cols.update(src_df.columns)
    for pop_data in populations.values():
        if pop_data["df"] is not None:
            all_source_cols.update(pop_data["df"].columns)
    # Known derived/metadata columns the pipeline creates
    # Static metadata columns always present in output
    known_derived = {
        "match_step", "match_tier", "name_score",
        "addr_score", "addr_street_match", "addr_comparison", "addr_tier",
        "reason_code", "rejection_step", "best_rejected_score",
    }
    # Dynamically add columns from recipe inherit[].as values
    for step in recipe.get("steps", []):
        for inh in step.get("inherit", []):
            if "as" in inh:
                known_derived.add(inh["as"])

    for tab_key in ("matched", "analysis"):
        for i, entry in enumerate(output_columns.get(tab_key, [])):
            if "field" not in entry and "fields" not in entry:
                errors.append(
                    f'output.columns.{tab_key}[{i}]: entry must have '
                    f'either "field" or "fields"'
                )
                continue
            if "field" in entry and "fields" in entry:
                errors.append(
                    f'output.columns.{tab_key}[{i}]: entry has both '
                    f'"field" and "fields" -- use one or the other'
                )
            if "field" in entry:
                f = entry["field"]
                if f not in all_source_cols and f not in known_derived:
                    errors.append(
                        f'output.columns.{tab_key}: field "{f}" not found in '
                        f"source data or known derived columns"
                    )
            if "fields" in entry:
                for f in entry["fields"]:
                    # Variant fields include _dst suffixed cols which won't
                    # exist until after the join. Only warn on base names
                    if not f.endswith("_dst") and f not in all_source_cols:
                        warnings.append(
                            f'output.columns.{tab_key}: variant field "{f}" '
                            f"not found in source data"
                        )

    return errors, warnings


def format_validation_summary(
    recipe: dict,
    sources: dict[str, pl.DataFrame],
    populations: dict[str, dict],
    errors: list[str],
    warnings: list[str],
    schema_warnings: list[str] | None = None,
) -> str:
    """Format a human-readable validation summary for --dry-run."""
    if schema_warnings:
        warnings = list(warnings) + schema_warnings
    lines = []
    lines.append(f"Recipe: {recipe.get('name', 'unnamed')}")
    lines.append("Schema: ✅ valid")
    lines.append("")

    lines.append("Sources:")
    for name, df in sources.items():
        src_cfg = recipe["sources"][name]
        src_label = src_cfg.get('file', f"{src_cfg.get('driver', 'sql')} query")
        lines.append(f"  {name}: {src_label} ({df.height} rows, {df.width} cols)")
    lines.append("")

    lines.append("Populations:")
    for pop_name, pop_data in populations.items():
        df = pop_data["df"]
        pop_cfg = pop_data["config"]
        row_count = df.height if df is not None else 0
        action = pop_cfg.get("action", "")
        label = " (excluded)" if action == "exclude" else ""
        filters = pop_cfg.get("filter", [])
        if filters:
            filter_parts = []
            for f in filters:
                if "field" in f:
                    filter_parts.append(f"{f['field']} {f['op']} {f.get('value', f.get('values', ''))}")
            if filter_parts:
                label += f" (filter: {', '.join(filter_parts)})"
        lines.append(f"  {pop_name}: {row_count} rows{label}")
    lines.append("")

    lines.append("Field validation:")
    for i, step in enumerate(recipe["steps"]):
        step_label = f'Step {i+1} "{step.get("name", "?")}"'
        lines.append(f"  {step_label}:")

        src_pop = step.get("source", "")
        dst_pop = step.get("destination", "")
        src_df = populations.get(src_pop, {}).get("df")
        dst_df = populations.get(dst_pop, {}).get("df")
        if dst_df is None and dst_pop in sources:
            dst_df = sources[dst_pop]

        for mf in step.get("match_fields", []):
            s_ok = src_df is not None and mf["source"] in src_df.columns
            d_ok = dst_df is not None and mf["destination"] in dst_df.columns
            lines.append(f"    match_fields.source: {mf['source']} {'✅' if s_ok else '❌'}")
            lines.append(f"    match_fields.destination: {mf['destination']} {'✅' if d_ok else '❌'}")

        if "address_support" in step:
            ac = step["address_support"]
            for af in ac.get("source", []):
                ok = src_df is not None and af in src_df.columns
                lines.append(f"    address_support.source: {af} {'✅' if ok else '⚠️'}")
            for af in ac.get("destination", []):
                ok = dst_df is not None and af in dst_df.columns
                lines.append(f"    address_support.destination: {af} {'✅' if ok else '⚠️'}")

        if "date_gate" in step:
            dg = step["date_gate"]
            dg_field = dg.get("field", "")
            applies_to = dg.get("applies_to", "destination")
            check_df = dst_df if applies_to == "destination" else src_df
            ok = check_df is not None and dg_field in check_df.columns
            lines.append(f"    date_gate: {dg_field} {'✅' if ok else '⚠️'}")

        for fi, filt in enumerate(step.get("filters", [])):
            filt_field = filt.get("field", "")
            applies_to = filt.get("applies_to", "destination")
            if applies_to in ("destination", "both"):
                check_df_f = dst_df
            else:
                check_df_f = src_df
            ok = check_df_f is not None and filt_field in check_df_f.columns
            lines.append(f"    filters[{fi}]: {filt_field} ({filt.get('op', '?')}, {applies_to}) {'✅' if ok else '⚠️'}")

        for inh in step.get("inherit", []):
            ok = dst_df is not None and inh["source"] in dst_df.columns
            lines.append(f"    inherit: {inh['source']} → {inh['as']} {'✅' if ok else '❌'}")

    lines.append("")

    if errors:
        lines.append(f"❌ {len(errors)} error(s):")
        for e in errors:
            lines.append(f"  {e}")
    if warnings:
        lines.append(f"⚠️  {len(warnings)} warning(s):")
        for w in warnings:
            lines.append(f"  {w}")

    if not errors and not warnings:
        lines.append("✅ All field references valid. Ready to run.")
    elif not errors:
        lines.append("\n✅ No critical errors. Ready to run (with warnings).")
    else:
        lines.append("\n❌ Critical errors found. Pipeline will not run.")

    return "\n".join(lines)
