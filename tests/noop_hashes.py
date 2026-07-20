"""Hash artifacts for recipes with no output.groups (Issue #97 no-op check).

Run on this branch and on main; the digests must be identical.
Usage: .venv/bin/python tests/noop_hashes.py <out_dir>
"""

import hashlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from matching import run_pipeline
from recipe import load_recipe

# Byte-stable recipes only, so a digest change means a real change.
# Excluded, and both verified to differ run-to-run on main alone:
#   config/recipes/multipop_comparison_rollup.yaml -- merged row order varies
#   same_pop_example / tie_breaker_example -- xlsx embeds a creation timestamp
RECIPES = [
    "tests/recipes/decision_record_test.yaml",
    "tests/recipes/merged_output_test.yaml",
    "tests/recipes/source_order_test.yaml",
]


def _main_module():
    spec = importlib.util.spec_from_file_location("m", ROOT / "src" / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(recipe_path, out_dir):
    recipe = load_recipe(str(ROOT / recipe_path))
    result = run_pipeline(recipe, base_dir=str(ROOT / "data"))
    slug = Path(recipe_path).stem
    target = out_dir / slug
    target.mkdir(parents=True, exist_ok=True)
    pops = result.get("populations", {})
    _main_module()._write_output(
        output_cfg=recipe["output"],
        matched_df=result["matched"],
        unmatched_df=result.get("unmatched"),
        output_path=str(target / "data.csv"),
        stats=result.get("stats", {}),
        recipe=recipe,
        recipe_file=Path(recipe_path).name,
        timing=result.get("timing"),
        source_df=pops.get("pop1"),
        source_key="vnd_id",
    )
    return target


if __name__ == "__main__":
    out_dir = Path(sys.argv[1])
    for recipe_path in RECIPES:
        target = run(recipe_path, out_dir)
        for artifact in sorted(target.glob("*")):
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            print(f"{digest}  {target.name}/{artifact.name}")
