"""
Address handling module for the relational matching framework.

Two-pass approach with optional libpostal:
- Pass 1: Classify tokens against static address patterns to extract
  street name. Weight street name higher in scoring.
- Pass 2: Fuzzy fallback on full string via RapidFuzz.

Parser modes (configurable per recipe):
- auto: use libpostal if installed, fall back to default
- libpostal: require it, fail if not available
- default: built-in tokenizer, zero external dependencies
"""

import json
import re
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz as rfuzz

from normalize import apply_tier

# ---------------------------------------------------------------------------
# libpostal availability check
# ---------------------------------------------------------------------------

try:
    from postal.parser import parse_address as _libpostal_parse
    LIBPOSTAL_AVAILABLE = True
except (ImportError, SystemError, OSError):
    LIBPOSTAL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Address pattern loading
# ---------------------------------------------------------------------------

_PATTERNS_CACHE: Optional[dict] = None


def _load_patterns(path: Optional[str] = None) -> dict:
    """Load address patterns from JSON config."""
    global _PATTERNS_CACHE
    use_cache = path is None
    if use_cache and _PATTERNS_CACHE is not None:
        return _PATTERNS_CACHE

    if path is None:
        path = str(Path(__file__).parent.parent / "config" / "address_patterns.json")

    with open(path) as f:
        data = json.load(f)

    # Build lookup sets for fast matching (lowercase)
    patterns = {
        "street_suffixes": {s.lower() for s in data.get("street_suffixes", [])},
        "unit_keywords": {s.lower() for s in data.get("unit_keywords", [])},
        "directionals": {s.lower() for s in data.get("directionals", [])},
        "state_codes": {s.lower() for s in data.get("state_codes", [])},
        "zip_patterns": [re.compile(p, re.IGNORECASE) for p in data.get("zip_patterns", [])],
    }

    if use_cache:
        _PATTERNS_CACHE = patterns
    return patterns


# ---------------------------------------------------------------------------
# Address variant building
# ---------------------------------------------------------------------------

def build_variants(*addrs: str) -> dict:
    """Build address variants from N address fields.

    Accepts 1+ positional string args (addr1, addr2, ..., addrN).
    Returns dict with: addr1_only, addr2_only, ..., addrN_only, addr_merged,
    and fields (list of individual field values for iteration).
    """
    cleaned = []
    for a in addrs:
        cleaned.append(str(a).strip() if a else "")

    merged = " ".join(c for c in cleaned if c).strip()

    result = {"addr_merged": merged, "fields": cleaned}
    for i, val in enumerate(cleaned, start=1):
        result[f"addr{i}_only"] = val

    return result


# ---------------------------------------------------------------------------
# Pass 1: Token classification (default built-in tokenizer)
# ---------------------------------------------------------------------------

def classify_tokens(address: str, patterns: Optional[dict] = None) -> dict:
    """Classify address tokens against known patterns.

    Returns dict with:
    - street_name: inferred street name (tokens before first suffix)
    - street_suffix: matched suffix token
    - unit: unit/floor/suite info
    - directionals: directional tokens found
    - state: state code if found
    - zip_code: zip if found
    - unclassified: remaining tokens
    - classified: bool indicating whether any patterns matched
    """
    if patterns is None:
        patterns = _load_patterns()

    tokens = address.lower().split()
    if not tokens:
        return {"classified": False, "street_name": "", "unclassified": []}

    result = {
        "street_name": "",
        "street_suffix": "",
        "unit": "",
        "directionals": [],
        "state": "",
        "zip_code": "",
        "unclassified": [],
        "classified": False,
    }

    # Classify each token
    classified_indices = set()
    suffix_index = None

    # Ambiguous tokens: some tokens appear in multiple categories.
    # Resolution: state codes only match in the last 3 tokens,
    # directionals only in the first 3 tokens (near street name),
    # unit keywords only consume next token if it looks numeric.
    last_n = max(0, len(tokens) - 3)

    for i, token in enumerate(tokens):
        clean_token = token.rstrip(".,;:")

        # Street suffixes: match anywhere
        if clean_token in patterns["street_suffixes"] and clean_token not in patterns["state_codes"]:
            if suffix_index is None:
                result["street_suffix"] = clean_token
                suffix_index = i
            classified_indices.add(i)
            result["classified"] = True

        # Ambiguous suffix/state (e.g., "ct") -- position determines
        elif clean_token in patterns["street_suffixes"] and clean_token in patterns["state_codes"]:
            if i >= last_n:
                result["state"] = clean_token
            else:
                if suffix_index is None:
                    result["street_suffix"] = clean_token
                    suffix_index = i
            classified_indices.add(i)
            result["classified"] = True

        elif clean_token in patterns["unit_keywords"] and (
            clean_token not in patterns["state_codes"] or i < last_n
        ):  # Ambiguous unit/state (e.g., "fl"): unit if early, state if late
            unit_parts = [clean_token]
            # Only consume next token if it looks like a unit number
            if i + 1 < len(tokens):
                next_token = tokens[i + 1].rstrip(".,;:")
                if re.match(r'^[\dA-Za-z]{1,5}$', next_token) and next_token not in patterns["state_codes"]:
                    unit_parts.append(next_token)
                    classified_indices.add(i + 1)
            result["unit"] = " ".join(unit_parts)
            classified_indices.add(i)
            result["classified"] = True

        elif clean_token in patterns["directionals"] and i < 3:
            # Directionals only near the start of the address
            result["directionals"].append(clean_token)
            classified_indices.add(i)
            result["classified"] = True

        elif clean_token in patterns["state_codes"] and i >= last_n:
            # State codes only near the end
            result["state"] = clean_token
            classified_indices.add(i)
            result["classified"] = True

        else:
            # Zip patterns: US 5-digit and 5+4 only
            # Only in the last 3 tokens to avoid matching street numbers
            if i >= last_n:
                for zip_pat in patterns["zip_patterns"]:
                    if zip_pat.fullmatch(clean_token):
                        result["zip_code"] = clean_token
                        classified_indices.add(i)
                        result["classified"] = True
                        break

    # Infer street name: tokens before first suffix (excluding directionals)
    if suffix_index is not None:
        street_tokens = []
        for i in range(suffix_index):
            if i not in classified_indices:
                street_tokens.append(tokens[i])
        result["street_name"] = " ".join(street_tokens)
    else:
        # No suffix found. Unclassified tokens might be the street
        result["unclassified"] = [tokens[i] for i in range(len(tokens))
                                   if i not in classified_indices]

    return result


# ---------------------------------------------------------------------------
# Pass 1 (libpostal mode)
# ---------------------------------------------------------------------------

def classify_tokens_libpostal(address: str) -> dict:
    """Classify address tokens using libpostal.

    Returns same structure as classify_tokens for compatibility.
    """
    if not LIBPOSTAL_AVAILABLE:
        raise RuntimeError("libpostal is not installed. Use parser='default' or 'auto'.")

    parsed = _libpostal_parse(address)
    result = {
        "street_name": "",
        "street_suffix": "",
        "unit": "",
        "directionals": [],
        "state": "",
        "zip_code": "",
        "unclassified": [],
        "classified": True,
    }

    parts = []
    for value, label in parsed:
        if label == "house_number":
            parts.insert(0, value)
        elif label == "road":
            parts.append(value)
        elif label in ("unit", "level"):
            result["unit"] = value
        elif label == "state":
            result["state"] = value
        elif label == "postcode":
            result["zip_code"] = value

    result["street_name"] = " ".join(parts)
    return result


# ---------------------------------------------------------------------------
# Address parsing dispatcher
# ---------------------------------------------------------------------------

def parse_address(address: str, parser: str = "auto",
                  patterns: Optional[dict] = None) -> dict:
    """Parse an address string into components.

    Args:
        address: Raw address string
        parser: 'auto' | 'libpostal' | 'default'
        patterns: Pre-loaded address patterns (for default mode)

    Returns:
        Classified token dict from either tokenizer
    """
    if parser == "libpostal":
        return classify_tokens_libpostal(address)
    elif parser == "auto":
        if LIBPOSTAL_AVAILABLE:
            return classify_tokens_libpostal(address)
        else:
            return classify_tokens(address, patterns)
    else:  # default
        return classify_tokens(address, patterns)


# ---------------------------------------------------------------------------
# Address scoring
# ---------------------------------------------------------------------------

def score_address_pair(addr_src: dict, addr_dst: dict,
                       tier: str = "clean",
                       parser: str = "auto",
                       aliases: Optional[dict] = None,
                       stopwords: Optional[list] = None,
                       street_weight: float = 0.6) -> dict:
    """Score a pair of address variants.

    Compares all combinations (merged<>merged, addr1<>addr2, etc.)
    and returns the best score with metadata.

    Args:
        addr_src: Source address variants from build_variants()
        addr_dst: Destination address variants from build_variants()
        tier: Normalization tier to apply ('raw', 'clean', 'normalized')
        parser: Address parser mode
        aliases: Alias dict for normalized tier
        stopwords: Stopword list for normalized tier
        street_weight: Weight for street name component (0.0-1.0, default 0.6)

    Returns:
        Dict with: best_score, best_comparison, street_match, tier_used,
        street_src, street_dst
    """
    # Build comparison pairs dynamically from N fields.
    # Specific field comparisons first (higher signal), merged last
    # (noisiest). On equal scores, first comparison wins.
    comparisons = []
    src_fields = addr_src.get("fields", [])
    dst_fields = addr_dst.get("fields", [])
    for si, sv in enumerate(src_fields, start=1):
        for di, dv in enumerate(dst_fields, start=1):
            comparisons.append((f"addr{si}<>addr{di}", sv, dv))
    comparisons.append(("merged<>merged", addr_src["addr_merged"], addr_dst["addr_merged"]))

    best = {
        "best_score": 0.0,
        "best_comparison": "",
        "street_match": False,
        "street_score": 0.0,
        "tier_used": tier,
        "street_src": "",
        "street_dst": "",
    }

    for comp_name, src_val, dst_val in comparisons:
        if not src_val or not dst_val:
            continue

        # Apply normalization tier
        src_norm = apply_tier(src_val, tier, aliases=aliases, stopwords=stopwords)
        dst_norm = apply_tier(dst_val, tier, aliases=aliases, stopwords=stopwords)

        if not src_norm or not dst_norm:
            continue

        # Full string fuzzy score
        full_score = rfuzz.token_sort_ratio(src_norm, dst_norm)

        # Parse addresses for street name comparison
        src_parsed = parse_address(src_norm, parser=parser)
        dst_parsed = parse_address(dst_norm, parser=parser)

        street_score = 0.0
        street_match = False
        if src_parsed["street_name"] and dst_parsed["street_name"]:
            street_score = rfuzz.ratio(
                src_parsed["street_name"].lower(),
                dst_parsed["street_name"].lower()
            )
            street_match = street_score >= 80

        # Weighted score: apply when both streets are parseable
        if src_parsed["street_name"] and dst_parsed["street_name"]:
            weighted = (street_score * street_weight) + (full_score * (1 - street_weight))
        else:
            weighted = full_score

        if weighted > best["best_score"]:
            best["best_score"] = weighted
            best["best_comparison"] = comp_name
            best["street_match"] = street_match
            best["street_score"] = street_score
            best["street_src"] = src_parsed["street_name"]
            best["street_dst"] = dst_parsed["street_name"]

    return best


# ---------------------------------------------------------------------------
# Multi-tier address scoring
# ---------------------------------------------------------------------------

def score_address_multi_tier(src_addrs: list, dst_addrs: list,
                              tiers: list = None,
                              parser: str = "auto",
                              aliases: Optional[dict] = None,
                              stopwords: Optional[list] = None,
                              street_weight: float = 0.6) -> dict:
    """Score addresses across multiple normalization tiers.

    Tries each tier in order, returns best result.
    Default tier order: raw -> clean -> normalized

    Args:
        src_addrs: List of source address field values
        dst_addrs: List of destination address field values
        tiers: List of tiers to try (default: ['raw', 'clean', 'normalized'])
        parser: Address parser mode
        aliases: For normalized tier
        stopwords: For normalized tier
        street_weight: Weight for street name component (0.0-1.0, default 0.6)

    Returns:
        Best score result across all tiers with tier_used indicated
    """
    if tiers is None:
        tiers = ["raw", "clean", "normalized"]

    src_variants = build_variants(*src_addrs)
    dst_variants = build_variants(*dst_addrs)

    best = {"best_score": 0.0, "tier_used": "none"}

    for tier in tiers:
        result = score_address_pair(
            src_variants, dst_variants,
            tier=tier, parser=parser,
            aliases=aliases, stopwords=stopwords,
            street_weight=street_weight,
        )
        if result["best_score"] > best["best_score"]:
            best = result

    return best
