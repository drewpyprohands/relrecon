"""
Recipe config loader and validator.

Loads YAML/JSON recipes, validates structure, resolves data sources,
and parses the filter DSL into Polars expressions.
"""

import json
import re
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
    # Apply to top-level steps (single-phase)
    for i, step in enumerate(recipe.get("steps", [])):
        recipe["steps"][i] = _deep_merge(defaults, step)
    # Apply to phase steps (multi-phase)
    for phase in recipe.get("phases", []):
        phase_defaults = phase.pop("step_defaults", defaults)
        if phase_defaults:
            for i, step in enumerate(phase.get("steps", [])):
                phase["steps"][i] = _deep_merge(phase_defaults, step)
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
                path = getattr(err, "json_path", None) or "$"
                # additionalProperties violations are warnings (typos, indent bugs)
                if err.validator == "additionalProperties":
                    warnings.append(f"{path}: {err.message}")
                else:
                    schema_errors.append(f"{path}: {err.message}")
            if schema_errors:
                # Improve opaque oneOf errors for output placement
                improved = []
                for e in schema_errors:
                    if "is not valid under any of the given schemas" in e:
                        if "phases" in recipe and "output" in recipe:
                            improved.append(
                                "Multi-phase recipes must not have a top-level "
                                "'output'. Define output per-phase instead."
                            )
                        elif "phases" not in recipe and "output" not in recipe:
                            improved.append(
                                "Single-phase recipes require a top-level "
                                "'output' block."
                            )
                        else:
                            improved.append(e)
                    else:
                        improved.append(e)
                detail = "\n  ".join(improved)
                raise ValueError(f"Recipe validation failed:\n  {detail}")

    # Collect all critical errors, raise once at the end
    critical: list[str] = []

    # Fallback for environments without jsonschema
    is_multi_phase = "phases" in recipe
    if is_multi_phase:
        required = ["name", "sources", "phases"]
    else:
        required = ["name", "sources", "populations", "steps", "output"]
    missing = [k for k in required if k not in recipe]
    if missing:
        critical.append(f"Recipe missing required fields: {missing}")

    # Output placement rules (ADR-003)
    if is_multi_phase:
        if "output" in recipe:
            critical.append(
                "Multi-phase recipes must not have a top-level 'output'. "
                "Define output per-phase instead."
            )
        phases_with_output = [
            i for i, p in enumerate(recipe.get("phases", []))
            if "output" in p
        ]
        if not phases_with_output and not missing:
            critical.append(
                "Multi-phase recipe has no output on any phase. "
                "At least one phase must define an 'output' block."
            )
        else:
            # Warn about phases without output
            for i, p in enumerate(recipe.get("phases", [])):
                if "output" not in p:
                    pname = p.get("name", f"Phase {i + 1}")
                    warnings.append(
                        f"{pname} has no output block -- its results won't be exported"
                    )

    # Enrichment mode validation
    source_names = set(recipe.get("sources", {}).keys())
    for phase in recipe.get("phases", []):
        pname = phase.get("name", "phase")
        pout = phase.get("output", {})
        if pout.get("mode") == "enriched":
            if not pout.get("enrich_source"):
                critical.append(
                    f"{pname}: mode=enriched requires 'enrich_source' "
                    f"(which source dataset to enrich)"
                )
            elif pout["enrich_source"] not in source_names:
                critical.append(
                    f"{pname}: enrich_source '{pout['enrich_source']}' "
                    f"not found in sources. Available: {sorted(source_names)}"
                )
            if not pout.get("enrich_key"):
                critical.append(
                    f"{pname}: mode=enriched requires 'enrich_key' "
                    f"(join column between source and matched results)"
                )
    if "output" in recipe and recipe["output"].get("mode") == "enriched":
        if not recipe["output"].get("enrich_source"):
            critical.append(
                "output: mode=enriched requires 'enrich_source'"
            )
        elif recipe["output"]["enrich_source"] not in source_names:
            critical.append(
                f"output: enrich_source '{recipe['output']['enrich_source']}' "
                f"not found in sources. Available: {sorted(source_names)}"
            )
        if not recipe["output"].get("enrich_key"):
            critical.append(
                "output: mode=enriched requires 'enrich_key'"
            )

    # matched_unmatched is single-phase only (multi-phase support deferred).
    if is_multi_phase:
        for i, p in enumerate(recipe.get("phases", [])):
            if "matched_unmatched" in p.get("output", {}):
                pname = p.get("name", f"Phase {i + 1}")
                critical.append(
                    f"{pname}: output.matched_unmatched is not supported in "
                    "multi-phase recipes. Use a single-phase recipe."
                )

    # Deprecation warnings
    def _check_output_block(output_block: dict, label: str) -> None:
        # matched_unmatched takes precedence over the deprecated emit_unmatched.
        if "matched_unmatched" in output_block and "emit_unmatched" in output_block:
            warnings.append(
                f"{label}: emit_unmatched is deprecated and ignored when "
                "matched_unmatched is set. matched_unmatched takes precedence. "
                "Remove emit_unmatched."
            )
        if "tabs" in output_block:
            warnings.append(
                f"{label}: 'tabs' is deprecated and ignored. "
                "Tab generation is automatic (Summary + Matched + Analysis)."
            )
        if output_block.get("format") == "xlsx" and "summary" not in output_block:
            warnings.append(
                f"{label} has format: xlsx but no summary key. "
                "Add summary: [md, xlsx] for a formatted report, "
                "or summary: none to silence this warning."
            )
        if (
            output_block.get("emit_unmatched")
            and "matched_unmatched" not in output_block
            and output_block.get("format") == "xlsx"
            and "xlsx" in resolve_summary_modes(output_block)
        ):
            warnings.append(
                f"{label}: emit_unmatched has no effect in xlsx report mode "
                "(format: xlsx + xlsx summary) -- unmatched records are already "
                "in the Analysis tab. Use format: csv or parquet to export them."
            )

    if "output" in recipe:
        _check_output_block(recipe["output"], "output")
    for phase in recipe.get("phases", []):
        pname = phase.get("name", "phase")
        if "output" in phase:
            _check_output_block(phase["output"], pname)

    for name, src in recipe.get("sources", {}).items():
        if "file" not in src and "loader" not in src:
            critical.append(f"Source '{name}' missing 'file' or 'loader' field")

    # Collect all steps (from phases or top-level)
    all_steps = []
    if is_multi_phase:
        for phase in recipe.get("phases", []):
            all_steps.extend(phase.get("steps", []))
    else:
        all_steps = recipe.get("steps", [])

    step_names_seen: dict[str, int] = {}
    for i, step in enumerate(all_steps):
        for k in ["name", "source", "destination", "match_fields"]:
            if k not in step:
                critical.append(f"Step {i} ('{step.get('name', '?')}') missing '{k}'")
        sname = step.get("name")
        if sname is not None:
            if sname == "unmatched":
                critical.append(
                    'Step name "unmatched" is reserved: the merged output view '
                    "uses it as the match_step sentinel for unmatched rows. "
                    "Rename this step."
                )
            if sname in step_names_seen:
                critical.append(
                    f'Duplicate step name "{sname}" (steps {step_names_seen[sname]} and {i}). '
                    'Each step must have a unique name.'
                )
            step_names_seen[sname] = i

    if "output" in recipe and "format" not in recipe["output"]:
        critical.append("Output missing 'format' field")

    # is_unmatched is reserved: it is auto-appended to merged artifacts only
    # and is not referenceable from output.columns.
    def _check_reserved_columns(output_block: dict, label: str) -> None:
        for key, entries in (output_block.get("columns") or {}).items():
            for i, entry in enumerate(entries or []):
                if not isinstance(entry, dict):
                    continue
                names = [entry.get("field")] + list(entry.get("fields", []) or [])
                where = "field" if "is_unmatched" in names else None
                if entry.get("header") == "is_unmatched":
                    where = where or "header"
                if where:
                    critical.append(
                        f'{label}.columns.{key}[{i}]: "is_unmatched" is a reserved '
                        f"column name and cannot be used as a {where}. It is "
                        "auto-appended as the final column of merged artifacts "
                        "only. Rename it."
                    )

    if "output" in recipe:
        _check_reserved_columns(recipe["output"], "output")
    for phase in recipe.get("phases", []):
        if "output" in phase:
            _check_reserved_columns(phase["output"], phase.get("name", "phase"))

    # final_rollup: single-phase terminal aggregation pass (Issue #67)
    phase_rollups = [
        p.get("output", {}).get("final_rollup")
        for p in recipe.get("phases", [])
    ]
    if is_multi_phase and any(phase_rollups):
        critical.append(
            "output.final_rollup is not supported in multi-phase recipes. "
            "It is a single-phase terminal pass; remove it or use a "
            "single-phase recipe."
        )
    rollup_buckets = recipe.get("output", {}).get("final_rollup", []) or []
    # Each bucket emits two columns: write_to and <write_to>_changed. No two
    # buckets may share either name -- overlap would silently overwrite one
    # bucket's output (including its audit flag) with another's.
    reserved: dict[str, int] = {}
    for i, bucket in enumerate(rollup_buckets):
        if not isinstance(bucket, dict):
            continue
        # steps is optional -- omit to apply the bucket to all steps.
        for sname in bucket.get("steps", []):
            if sname not in step_names_seen:
                critical.append(
                    f'output.final_rollup[{i}]: step "{sname}" names no '
                    f"existing step. Known steps: {sorted(step_names_seen)}"
                )
        write_to = bucket.get("write_to", "rolled_supplier_id")
        cols = (write_to, f"{write_to}_changed")
        hit = next((c for c in cols if c in reserved), None)
        if hit is not None:
            critical.append(
                f'output.final_rollup: buckets {reserved[hit]} and {i} both '
                f'produce column "{hit}". Each bucket needs a unique write_to '
                "(its <write_to>_changed flag column is reserved too)."
            )
        for c in cols:
            reserved[c] = i

    # decision_record / compare_columns: presentation-layer output features
    # (Issue #93). Both are global-output-only, like matched_unmatched.
    if is_multi_phase:
        blocks = [("output", recipe.get("output", {}) or {})]
        for i, p in enumerate(recipe.get("phases", [])):
            blocks.append((p.get("name", f"Phase {i + 1}"), p.get("output", {}) or {}))
        for label, block in blocks:
            for key in ("decision_record", "compare_columns", "groups"):
                if key in block:
                    critical.append(
                        f"{label}: output.{key} is not supported in multi-phase "
                        "recipes. It is a global presentation-layer pass; use a "
                        "single-phase recipe."
                    )

    out_cfg = recipe.get("output", {}) or {}
    decision_record = out_cfg.get("decision_record")
    compare_entries = out_cfg.get("compare_columns") or []
    compare_names = compare_output_names(out_cfg)
    # Names no compare output may take. is_unmatched is the merged flag column,
    # "unmatched" the merged match_step sentinel.
    taken: dict[str, str] = {
        "is_unmatched": "the reserved merged-view flag column",
        "unmatched": "the reserved merged-view match_step sentinel",
    }
    for c in _STATIC_DERIVED_COLUMNS:
        taken[c] = "a pipeline metadata column"
    # Every column this recipe's derived paths produce, with where it came
    # from -- used both to police compare output names and, in reverse, to
    # stop a derived column from claiming a generated name.
    derived_sources: list[tuple[str, str]] = []
    for step in all_steps:
        for inh in step.get("inherit", []):
            if "as" in inh:
                derived_sources.append(
                    (inh["as"], f'step "{step.get("name", "?")}" inherit.as')
                )
                taken[inh["as"]] = "a derived (inherit) column"
    for i, bucket in enumerate(rollup_buckets):
        if isinstance(bucket, dict):
            wt = bucket.get("write_to", "rolled_supplier_id")
            derived_sources.append((wt, f"output.final_rollup[{i}].write_to"))
            derived_sources.append(
                (f"{wt}_changed", f"output.final_rollup[{i}] audit flag")
            )
            taken[wt] = "a final_rollup output column"
            taken[f"{wt}_changed"] = "a final_rollup audit column"
    dr_names = decision_record_columns(out_cfg)
    # Capture what each write_to name already meant before it is claimed --
    # overwriting first would hide the very collision we are looking for.
    dr_clashes = {n: taken[n] for n in dr_names if n in taken}
    for c in dr_names:
        taken[c] = "a decision_record output column"
    group_names = groups_columns(out_cfg)
    group_clashes = {n: taken[n] for n in group_names if n in taken}
    for c in group_names:
        taken[c] = "the groups output column"

    # Columns the presentation layer generates. Nothing else may produce them
    # and nothing may read them back as an input.
    generated: dict[str, str] = {
        c: "a compare_columns output column" for c in compare_names
    }
    for c in dr_names:
        generated[c] = "a decision_record output column"
    for c in group_names:
        generated[c] = "the groups output column"

    for name, where in derived_sources:
        if name in generated:
            critical.append(
                f'{where}: "{name}" is {generated[name]}. A derived column '
                "cannot take a generated column's name -- the computation "
                "would overwrite it at output time. Rename it."
            )

    seen_compare: dict[str, int] = {}
    for i, entry in enumerate(compare_entries):
        if not isinstance(entry, dict):
            continue
        name = entry.get("output")
        if not name:
            continue
        if name in taken:
            critical.append(
                f'output.compare_columns[{i}]: output "{name}" collides with '
                f"{taken[name]}. Choose a different output name."
            )
        if name in seen_compare:
            critical.append(
                f'output.compare_columns: entries {seen_compare[name]} and {i} '
                f'both emit column "{name}". Each output name must be unique.'
            )
        seen_compare[name] = i

    # Operands are read before any generated column exists, so naming one
    # (including this entry's own output) is a cross-feature reference.
    for i, entry in enumerate(compare_entries):
        if not isinstance(entry, dict):
            continue
        for role in ("left", "right"):
            col = entry.get(role)
            if col in generated:
                critical.append(
                    f'output.compare_columns[{i}]: {role} "{col}" names '
                    f"{generated[col]}. Cross-feature references are not "
                    "supported -- use a source or derived column."
                )

    if decision_record is not None:
        if not decision_record.get("write_to"):
            critical.append(
                "output.decision_record: write_to is required. It names the "
                "decided column and its <write_to>_src companion."
            )
        candidates = decision_record.get("candidates") or []
        if len(candidates) < 2:
            critical.append(
                f"output.decision_record.candidates needs at least 2 entries "
                f"(got {len(candidates)}). A coalesce over one column is a "
                "rename, not a decision."
            )
        select = decision_record.get("select", "first")
        if select not in ("first", "min", "max"):
            critical.append(
                f'output.decision_record: select "{select}" is not one of '
                "first, min, max."
            )
        for c in candidates:
            if c in generated:
                critical.append(
                    f'output.decision_record: candidate "{c}" names '
                    f"{generated[c]}. Cross-feature references are not "
                    "supported -- use a source or derived column."
                )
        # write_to collisions are policed by the same map as compare outputs.
        for name in dr_names:
            clash = dr_clashes.get(name)
            if clash:
                critical.append(
                    f'output.decision_record: write_to emits "{name}", which '
                    f"collides with {clash}. Choose a different write_to."
                )
            if name in compare_names:
                critical.append(
                    f'output.decision_record: write_to emits "{name}", which '
                    "collides with a compare_columns output. Choose a "
                    "different write_to."
                )

    groups_cfg = out_cfg.get("groups")
    if groups_cfg is not None:
        if not groups_cfg.get("file"):
            critical.append(
                "output.groups: file is required. It names the groups.json "
                "holding the group definitions."
            )
        mode = groups_cfg.get("mode", "all_match")
        if mode not in ("all_match", "first_match"):
            critical.append(
                f'output.groups: mode "{mode}" is not one of all_match, '
                "first_match."
            )
        for name in group_names:
            clash = group_clashes.get(name)
            if clash:
                critical.append(
                    f'output.groups: emits "{name}", which collides with '
                    f"{clash}. \"{name}\" is reserved for group tagging."
                )
            if name in compare_names:
                critical.append(
                    f'output.groups: emits "{name}", which collides with a '
                    f"compare_columns output. \"{name}\" is reserved for "
                    "group tagging."
                )

    # The computed columns are matched-side only: they are evaluated against
    # matched/merged rows, so naming them on the analysis side is meaningless.
    computed = set(output_computed_columns(out_cfg))
    for i, entry in enumerate((out_cfg.get("columns") or {}).get("analysis") or []):
        if not isinstance(entry, dict):
            continue
        field = entry.get("field")
        if field in computed:
            critical.append(
                f'output.columns.analysis[{i}]: "{field}" is a presentation-'
                "layer output column (decision_record, compare_columns or "
                "groups). These are matched-view only -- reference them from "
                "output.columns.matched instead."
            )

    source_pops = {step["source"] for step in all_steps if "source" in step}
    dest_pops = {step["destination"] for step in all_steps if "destination" in step}

    # Collect all populations across phases or top-level
    all_populations = {}
    if is_multi_phase:
        for phase in recipe.get("phases", []):
            all_populations.update(phase.get("populations", {}))
    else:
        all_populations = recipe.get("populations", {})

    for pop_name in source_pops:
        pop_cfg = all_populations.get(pop_name, {})
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
        pop_cfg = all_populations.get(pop_name, {})
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


# ---------------------------------------------------------------------------
# Global exclusions file
# ---------------------------------------------------------------------------

def load_exclusions(exclusions_cfg, base_dir: str = ".") -> dict[str, list[str]]:
    """Load the global exclusions CSV into {step_name: [id, ...]}.

    ``exclusions_cfg`` is either a path string or an object
    ``{file, id_column?}``. The CSV needs a ``step`` column, one id column
    (name it whatever your data uses), and an optional ``note`` column. The
    id column is resolved as: ``id_column`` if set, else ``vnd_id`` if
    present, else the sole remaining non-note column. A header row is
    required; blank rows and rows missing step or id are skipped. Order is
    preserved.

    The id values are matched against each step's exclude field (the
    population ``record_key`` by default), so the header name is only a label.
    """
    import csv

    if isinstance(exclusions_cfg, str):
        path, id_column = exclusions_cfg, None
    else:
        path = exclusions_cfg.get("file")
        id_column = exclusions_cfg.get("id_column")
    if not path:
        raise ValueError("exclusions requires a 'file' path")

    p = Path(path)
    if not p.exists():
        p = Path(base_dir) / path
    if not p.exists():
        raise FileNotFoundError(f"Exclusions file not found: {path}")

    result: dict[str, list[str]] = {}
    with open(p, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        cols = {c.strip().lower(): c for c in fields}
        if "step" not in cols:
            raise ValueError(
                f"Exclusions file {p} must have a 'step' column; got {fields}"
            )
        id_col = _resolve_exclusion_id_col(p, cols, fields, id_column)
        step_col = cols["step"]
        for row in reader:
            step = (row.get(step_col) or "").strip()
            val = (row.get(id_col) or "").strip()
            if step and val:
                result.setdefault(step, []).append(val)
    return result


def _resolve_exclusion_id_col(p, cols: dict, fields: list, id_column) -> str:
    """Resolve which CSV column holds the exclusion ids."""
    if id_column:
        key = id_column.strip().lower()
        if key not in cols:
            raise ValueError(
                f"Exclusions file {p} has no '{id_column}' column; got {fields}"
            )
        return cols[key]
    if "vnd_id" in cols:
        return cols["vnd_id"]
    candidates = [orig for low, orig in cols.items() if low not in ("step", "note")]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(
        f"Exclusions file {p}: could not infer the id column from {fields}. "
        f"Set exclusions.id_column to the column holding the record ids."
    )


def apply_exclusions(recipe: dict, exclusions: dict[str, list[str]]) -> list[str]:
    """Route exclusion rows into each named step's exclude mechanism.

    Merges values into ``step["exclude"]["values"]`` (creating the block if
    absent), so any inline per-step exclude still applies. Returns a list of
    step names present in the file but not found in the recipe.
    """
    if not exclusions:
        return []

    steps = list(recipe.get("steps", []))
    for phase in recipe.get("phases", []):
        steps.extend(phase.get("steps", []))

    seen = {step.get("name") for step in steps}
    for step in steps:
        vals = exclusions.get(step.get("name"))
        if not vals:
            continue
        exc = step.setdefault("exclude", {})
        exc["values"] = list(exc.get("values", [])) + list(vals)

    return [name for name in exclusions if name not in seen]


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
    base_dir: str = ".",
) -> tuple[list[str], list[str]]:
    """Validate all recipe field references against loaded DataFrames.

    Args:
        recipe: Parsed recipe dict
        sources: {name: DataFrame} from loaded CSV/Parquet files
        populations: {name: {config, df, source}} after filtering
        base_dir: Data base dir, for resolving sidecar files (groups.json)

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

    # Get populations and steps for multi-phase or single-phase
    if "phases" in recipe:
        # For multi-phase, only validate the populations passed in
        recipe_populations = {}
        recipe_steps = []
    else:
        recipe_populations = recipe["populations"]
        recipe_steps = recipe["steps"]

    # Validate population filter fields
    for pop_name, pop_cfg in recipe_populations.items():
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
    for i, step in enumerate(recipe_steps):
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
    # Known derived/metadata columns the pipeline creates (static metadata +
    # inherit[].as names + final_rollup write_to/_changed flags).
    known_derived = known_derived_columns(recipe)
    # Columns that exist before the rollup runs (source + static + inherited);
    # write_to must not collide with any of these (checked below).
    preexisting_cols = all_source_cols | known_derived_columns(recipe, include_rollup=False)

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
                # _dst/_dst2 columns are created at runtime by joins
                is_join_col = "_dst" in f
                if f not in all_source_cols and f not in known_derived:
                    if is_join_col:
                        pass  # skip -- runtime join column
                    else:
                        errors.append(
                            f'output.columns.{tab_key}: field "{f}" not found in '
                            f"source data or known derived columns"
                        )
            if "fields" in entry:
                for f in entry["fields"]:
                    # Variant fields include _dst/_dst2 suffixed cols which
                    # won't exist until after the join. Only warn on base names
                    if "_dst" not in f and f not in all_source_cols:
                        warnings.append(
                            f'output.columns.{tab_key}: variant field "{f}" '
                            f"not found in source data"
                        )

    # decision_record / compare_columns: data-aware half of the Issue #93
    # rules. Name collisions against actual source columns and candidate
    # existence can only be checked once the sources are loaded.
    out_cfg = recipe.get("output", {}) or {}
    for name in decision_record_columns(out_cfg):
        if name in all_source_cols:
            errors.append(
                f'output.decision_record: write_to emits "{name}", which '
                "collides with an existing source column. Choose a different "
                "write_to."
            )
    for i, entry in enumerate(out_cfg.get("compare_columns") or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("output") in all_source_cols:
            errors.append(
                f'output.compare_columns[{i}]: output "{entry["output"]}" '
                "collides with an existing source column. Choose a different "
                "output name."
            )
        for role in ("left", "right"):
            col = entry.get(role)
            if col and "_dst" not in col and col not in (all_source_cols | known_derived):
                errors.append(
                    f'output.compare_columns[{i}]: {role} "{col}" not found in '
                    f"source data or known derived columns"
                )

    for c in (out_cfg.get("decision_record") or {}).get("candidates") or []:
        if "_dst" not in c and c not in (all_source_cols | known_derived):
            errors.append(
                f'output.decision_record: candidate "{c}" not found in source '
                f"data or known derived columns"
            )

    # groups: the file is read here rather than in validate_recipe because
    # match_columns can only be checked against loaded source data. Content
    # errors raise (named, zero artifacts); match_columns misses join the
    # collected errors so the author sees every miss at once.
    for name in groups_columns(out_cfg):
        if name in all_source_cols:
            errors.append(
                f'output.groups: emits "{name}", which collides with an '
                f'existing source column. "{name}" is reserved for group '
                "tagging -- rename the source column."
            )
    if (out_cfg.get("groups") or {}).get("file"):
        group_defs, group_warnings = load_groups(out_cfg, base_dir)
        warnings.extend(group_warnings)
        for group in group_defs:
            for col in group.get("match_columns") or []:
                if col not in all_source_cols:
                    errors.append(
                        f'output.groups: group "{group.get("group_name")}" '
                        f'match_columns names "{col}", which is not a source '
                        "column."
                    )

    # final_rollup: group_key/target must resolve to a matched-output column
    available = all_source_cols | known_derived
    for i, bucket in enumerate(recipe.get("output", {}).get("final_rollup", []) or []):
        if not isinstance(bucket, dict):
            continue
        for role in ("group_key", "target"):
            col = bucket.get(role)
            if col and "_dst" not in col and col not in available:
                errors.append(
                    f'output.final_rollup[{i}]: {role} "{col}" not found in '
                    f"source data or known derived columns"
                )

    # write_to must name a NEW column. Colliding with an existing source,
    # derived, or target column would silently overwrite it, and the
    # <write_to>_changed flag would be structurally always-False (Issue #67).
    for i, bucket in enumerate(recipe.get("output", {}).get("final_rollup", []) or []):
        if not isinstance(bucket, dict):
            continue
        write_to = bucket.get("write_to", "rolled_supplier_id")
        if write_to in preexisting_cols:
            errors.append(
                f'output.final_rollup[{i}]: write_to "{write_to}" collides with '
                f"an existing source/derived/target column. It would overwrite "
                f"that column and force {write_to}_changed to always-False. "
                f"Choose a new column name."
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

    # Collect all populations config (for _previous_matched detection)
    all_populations = recipe.get("populations", {})
    if not all_populations and "phases" in recipe:
        for phase in recipe["phases"]:
            all_populations.update(phase.get("populations", {}))

    lines.append("Field validation:")
    all_steps = recipe.get("steps", [])
    if not all_steps and "phases" in recipe:
        for phase in recipe["phases"]:
            all_steps.extend(phase.get("steps", []))
    for i, step in enumerate(all_steps):
        step_label = f'Step {i+1} "{step.get("name", "?")}"'
        lines.append(f"  {step_label}:")

        src_pop = step.get("source", "")
        dst_pop = step.get("destination", "")
        src_df = populations.get(src_pop, {}).get("df")
        dst_df = populations.get(dst_pop, {}).get("df")
        if dst_df is None and dst_pop in sources:
            dst_df = sources[dst_pop]

        # Track whether populations come from _previous_matched
        src_is_runtime = all_populations.get(src_pop, {}).get("source") == "_previous_matched"
        dst_is_runtime = all_populations.get(dst_pop, {}).get("source") == "_previous_matched"

        for mf in step.get("match_fields", []):
            if src_is_runtime:
                s_mark = "⏭️ (runtime)"
            elif src_df is not None and mf["source"] in src_df.columns:
                s_mark = "✅"
            else:
                s_mark = "❌"
            if dst_is_runtime:
                d_mark = "⏭️ (runtime)"
            elif dst_df is not None and mf["destination"] in dst_df.columns:
                d_mark = "✅"
            else:
                d_mark = "❌"
            lines.append(f"    match_fields.source: {mf['source']} {s_mark}")
            lines.append(f"    match_fields.destination: {mf['destination']} {d_mark}")

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


def normalize_formats(output_cfg: dict, default: str = "xlsx") -> list[str]:
    """Resolve output.format into an ordered, deduped list of format strings."""
    raw = output_cfg.get("format", default)
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        seen: list[str] = []
        for f in raw:
            if f not in seen:
                seen.append(f)
        return seen
    return [default]


def resolve_matched_unmatched(output_cfg: dict) -> list[str] | None:
    """Resolve output.matched_unmatched into a list of view modes, None if absent."""
    raw = output_cfg.get("matched_unmatched")
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        seen: list[str] = []
        for m in raw:
            if m not in seen:
                seen.append(m)
        return seen
    return None


def decision_record_columns(output_cfg: dict) -> list[str]:
    """The two columns output.decision_record emits, per its write_to."""
    cfg = output_cfg.get("decision_record") or {}
    write_to = cfg.get("write_to")
    if not write_to:
        return []
    return [write_to, f"{write_to}_src"]


# The single column output.groups emits. Reserved: nothing else may produce
# it and nothing may read it back. Rollup ids will emit as separate scalar
# columns in a follow-up -- this name stays the presentation tag.
GROUP_COLUMN = "group"

# Group keys with meaning in v1. Anything else is staged for a later issue
# and ignored with one load-time warning.
_GROUP_KNOWN_KEYS = {
    "group_name", "regex", "exclude_regex", "values", "match_columns",
}


def groups_columns(output_cfg: dict) -> list[str]:
    """The column output.groups emits ([] when unconfigured)."""
    return [GROUP_COLUMN] if (output_cfg.get("groups") or {}).get("file") else []


def load_groups(output_cfg: dict, base_dir: str = ".") -> tuple[list[dict], list[str]]:
    """Read and validate the groups.json named by output.groups.file.

    Returns (groups, warnings). Raises ValueError with a named message for
    every load-time failure, so no artifact is written against a bad file.
    Path resolution follows the aliases.json / stopwords.json convention:
    literal first, then relative to the data base dir.
    """
    cfg = output_cfg.get("groups") or {}
    ref = cfg.get("file")
    if not ref:
        return [], []

    path = Path(ref)
    if not path.exists():
        path = Path(base_dir) / ref
    if not path.exists():
        raise ValueError(
            f'output.groups: file "{ref}" not found (tried "{ref}" and '
            f'"{Path(base_dir) / ref}").'
        )
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise ValueError(f'output.groups: cannot read "{path}": {exc}') from None
    except json.JSONDecodeError as exc:
        raise ValueError(
            f'output.groups: "{path}" is not valid JSON (line {exc.lineno}, '
            f"column {exc.colno}): {exc.msg}"
        ) from None

    groups = raw.get("groups") if isinstance(raw, dict) else None
    if not isinstance(groups, list) or not groups:
        raise ValueError(
            f'output.groups: "{path}" has no non-empty "groups" list. '
            "Expected {\"groups\": [ {...} ]}."
        )

    warnings: list[str] = []
    extra_keys: list[str] = []
    seen: dict[str, int] = {}
    errors: list[str] = []

    for i, group in enumerate(groups):
        if not isinstance(group, dict):
            errors.append(f"output.groups: entry {i} is not an object.")
            continue
        name = group.get("group_name")
        label = f'"{name}"' if name else f"entry {i}"
        if not name:
            errors.append(f"output.groups: entry {i} has no group_name.")
        elif name in seen:
            errors.append(
                f'output.groups: group_name "{name}" is used by entries '
                f"{seen[name]} and {i}. Group names must be unique."
            )
        else:
            seen[name] = i

        has_values = bool(group.get("values"))
        for key in ("regex", "exclude_regex"):
            pattern = group.get(key)
            if pattern is None:
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(
                    f"output.groups: group {label} has an invalid {key} "
                    f'"{pattern}": {exc}'
                )
        if not group.get("regex") and not has_values:
            errors.append(
                f"output.groups: group {label} has neither regex nor values. "
                "A group needs at least one matching rule."
            )
        cols = group.get("match_columns")
        if not isinstance(cols, list) or not cols:
            errors.append(
                f"output.groups: group {label} has no match_columns. "
                "It is required and must name at least one source column."
            )
        extra_keys.extend(
            f"{label}.{k}" for k in group if k not in _GROUP_KNOWN_KEYS
        )

    if errors:
        if len(errors) == 1:
            raise ValueError(errors[0])
        detail = "\n  - ".join(errors)
        raise ValueError(
            f"output.groups: {len(errors)} errors in {path}:\n  - {detail}"
        )

    if extra_keys:
        warnings.append(
            f'output.groups: ignoring {len(extra_keys)} unrecognized key(s) in '
            f"{path}: {', '.join(extra_keys)}. Staged for a later issue; they "
            "have no effect in this version."
        )
    return groups, warnings


def compare_output_names(output_cfg: dict) -> list[str]:
    """Ordered output.compare_columns[].output names ([] when unconfigured)."""
    return [
        e["output"]
        for e in (output_cfg.get("compare_columns") or [])
        if isinstance(e, dict) and e.get("output")
    ]


def output_computed_columns(output_cfg: dict) -> list[str]:
    """Every column the presentation-layer computations emit, in emit order."""
    return (
        decision_record_columns(output_cfg)
        + compare_output_names(output_cfg)
        + groups_columns(output_cfg)
    )


# Static metadata columns the pipeline always creates in matched output.
_STATIC_DERIVED_COLUMNS = {
    "match_step", "match_tier", "name_score",
    "addr_score", "addr_street_match", "addr_comparison", "addr_tier",
    "reason_code", "rejection_step", "best_rejected_score",
}


def known_derived_columns(recipe: dict, include_rollup: bool = True) -> set[str]:
    """Return the set of derived/metadata columns the pipeline may create."""
    known = set(_STATIC_DERIVED_COLUMNS)
    steps = list(recipe.get("steps", []))
    if not steps and "phases" in recipe:
        for phase in recipe["phases"]:
            steps.extend(phase.get("steps", []))
    for step in steps:
        for inh in step.get("inherit", []):
            if "as" in inh:
                known.add(inh["as"])
    if include_rollup:
        for bucket in recipe.get("output", {}).get("final_rollup", []) or []:
            if isinstance(bucket, dict):
                write_to = bucket.get("write_to", "rolled_supplier_id")
                known.add(write_to)
                known.add(f"{write_to}_changed")
    known.update(output_computed_columns(recipe.get("output", {}) or {}))
    return known


def resolve_summary_modes(output_cfg: dict) -> list[str]:
    """Resolve the summary modes from an output config block.

    Returns a list of enabled summary modes (subset of ['md', 'xlsx']).
    No implicit defaults -- omitting summary key means no summary.
    """
    raw = output_cfg.get("summary")
    if raw is None:
        return []
    if raw == "none":
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [m for m in raw if m in ("md", "xlsx")]
    return []
