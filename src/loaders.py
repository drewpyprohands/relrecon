"""
Pluggable data source loaders.

Each loader returns a pl.DataFrame with all values cast to String.
Database drivers are lazy-imported so the engine doesn't require
them unless a recipe actually uses one.
"""

import getpass
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL = "24h"
CACHE_DIR = ".cache"


def _interpolate_env(value):
    if not isinstance(value, str):
        return value
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )


def _interpolate_dict(d):
    if isinstance(d, dict):
        return {k: _interpolate_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_interpolate_dict(v) for v in d]
    return _interpolate_env(d)


def _needs_prompt(value):
    if not value:
        return True
    return isinstance(value, str) and re.match(r"^\$\{.+\}$", value)


def _parse_ttl(ttl_str: str) -> float | None:
    if not ttl_str or str(ttl_str).lower() in ("off", "false", "none", "0"):
        return None

    s = str(ttl_str).strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}

    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            try:
                return float(s[:-1]) * mult
            except ValueError as err:
                raise ValueError(f"Invalid cache_ttl: {ttl_str}") from err

    try:
        return float(s)
    except ValueError as err:
        raise ValueError(f"Invalid cache_ttl: {ttl_str}") from err


def _cache_key(source_config: dict) -> str:
    conn = dict(source_config.get("connection", {}))
    conn.pop("password", None)
    raw = json.dumps({
        "driver": source_config.get("driver", ""),
        "connection": conn,
        "query": source_config.get("query", ""),
        "url": source_config.get("url", ""),
        "url_from": source_config.get("url_from", {}),
    }, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def _cache_ext(source_config: dict) -> str:
    fmt = str(source_config.get("cache_format", "parquet")).lower()
    if fmt in ("csv", "tsv"):
        return ".csv"
    return ".parquet"


def _get_cache_path(source_config: dict, base_dir: str,
                    recipe_name: str = "", source_name: str = "") -> Path:
    key = _cache_key(source_config)
    date_str = time.strftime("%Y%m%d")
    ext = _cache_ext(source_config)

    parts = []
    if recipe_name:
        parts.append(re.sub(r"[^a-z0-9]+", "_", recipe_name.lower()).strip("_"))
    if source_name:
        parts.append(re.sub(r"[^a-z0-9]+", "_", source_name.lower()).strip("_"))
    parts.append(date_str)
    parts.append(key)

    return Path(base_dir) / CACHE_DIR / ("_".join(parts) + ext)


def _find_cached_file(source_config: dict, base_dir: str) -> Path | None:
    key = _cache_key(source_config)
    ext = _cache_ext(source_config)
    cache_dir = Path(base_dir) / CACHE_DIR
    if not cache_dir.exists():
        return None
    for f in cache_dir.glob(f"*_{key}{ext}"):
        return f
    return None


def _read_cache(source_config: dict, base_dir: str,
                recipe_name: str = "", source_name: str = "") -> pl.DataFrame | None:
    ttl_seconds = _parse_ttl(source_config.get("cache_ttl", DEFAULT_CACHE_TTL))
    if ttl_seconds is None:
        return None

    cache_path = _find_cached_file(source_config, base_dir)
    if cache_path is None:
        return None

    age = time.time() - cache_path.stat().st_mtime
    if age > ttl_seconds:
        logger.info("Cache expired (%.0fs old, ttl=%.0fs): %s",
                    age, ttl_seconds, cache_path.name)
        cache_path.unlink(missing_ok=True)
        return None

    logger.info("Cache hit (%.0fs old): %s", age, cache_path.name)
    print(f"    Using cache: {cache_path.name} ({age/3600:.1f}h old)", flush=True)
    ext = _cache_ext(source_config)
    if ext == ".csv":
        return pl.read_csv(str(cache_path), infer_schema_length=0)
    return pl.read_parquet(str(cache_path))


def _write_cache(df: pl.DataFrame, source_config: dict, base_dir: str,
                 recipe_name: str = "", source_name: str = ""):
    ttl_seconds = _parse_ttl(source_config.get("cache_ttl", DEFAULT_CACHE_TTL))
    if ttl_seconds is None:
        return

    key = _cache_key(source_config)
    ext = _cache_ext(source_config)
    cache_dir = Path(base_dir) / CACHE_DIR
    if cache_dir.exists():
        for old in cache_dir.glob(f"*_{key}{ext}"):
            old.unlink(missing_ok=True)

    cache_path = _get_cache_path(source_config, base_dir, recipe_name, source_name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if ext == ".csv":
        df.write_csv(str(cache_path))
    else:
        df.write_parquet(str(cache_path))
    logger.info("Cached %d rows to %s", df.height, cache_path.name)


def _rows_to_dataframe(columns: list[str], rows: list) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(
            {col: [] for col in columns}
        ).cast({col: pl.String for col in columns})

    data = {
        col: [str(row[i]) if row[i] is not None else None for row in rows]
        for i, col in enumerate(columns)
    }
    return pl.DataFrame(data)


def load_file(source_config: dict, base_dir: str = ".", **kwargs) -> pl.DataFrame:
    file_path = Path(base_dir) / source_config["file"]
    if not file_path.exists():
        raise FileNotFoundError(f"Data file not found: {file_path}")

    suffix = file_path.suffix.lower()
    columns = source_config.get("columns")

    if suffix == ".parquet":
        df = pl.read_parquet(str(file_path), columns=columns)
    elif suffix in (".csv", ".tsv"):
        df = pl.read_csv(str(file_path), infer_schema_length=0)
        if columns:
            df = df.select(columns)
    else:
        raise ValueError(f"Unsupported format: {suffix}")

    return df


def load_sql(source_config: dict, base_dir: str = ".",
             recipe_name: str = "", source_name: str = "") -> pl.DataFrame:
    config = _interpolate_dict(source_config)
    driver = config.get("driver", "").lower()
    query = config.get("query", "").strip()
    conn_config = config.get("connection", {})

    if not query:
        raise ValueError("SQL loader requires a 'query' field")
    if not driver:
        raise ValueError("SQL loader requires a 'driver' field")

    cached = _read_cache(config, base_dir, recipe_name, source_name)
    if cached is not None:
        columns = config.get("columns")
        if columns:
            cached = cached.select(columns)
        return cached

    if driver == "sqlite":
        df = _load_sqlite(conn_config, query, base_dir)
    elif driver in ("postgresql", "postgres"):
        df = _load_postgresql(conn_config, query)
    elif driver == "trino":
        df = _load_trino(conn_config, query)
    else:
        raise ValueError(
            f"Unsupported SQL driver: {driver}. "
            f"Available: sqlite, postgresql, trino"
        )

    _write_cache(df, config, base_dir, recipe_name, source_name)

    columns = config.get("columns")
    if columns:
        df = df.select(columns)

    return df


def _load_trino(conn_config: dict, query: str) -> pl.DataFrame:
    try:
        from trino.auth import BasicAuthentication
        from trino.dbapi import connect
    except ImportError as err:
        raise ImportError("Trino driver not installed. Run: pip install trino") from err

    user = conn_config.get("user", "")
    password = conn_config.get("password")

    if _needs_prompt(user):
        user = input("Trino user: ")
    if password is not None and _needs_prompt(password):
        password = getpass.getpass("Trino password: ")

    auth = BasicAuthentication(user, password) if user and password else None

    conn_kwargs = {
        "host": conn_config.get("host", "localhost"),
        "port": int(conn_config.get("port", 8080)),
        "user": user or None,
        "catalog": conn_config.get("catalog"),
        "schema": conn_config.get("schema"),
    }
    if auth:
        conn_kwargs["auth"] = auth
    if conn_config.get("http_scheme"):
        conn_kwargs["http_scheme"] = conn_config["http_scheme"]
    if conn_config.get("verify") is not None:
        v = conn_config["verify"]
        if isinstance(v, str):
            if v.lower() in ("false", "0", "no"):
                v = False
            elif v.lower() in ("true", "1", "yes"):
                v = True
        conn_kwargs["verify"] = v

    conn_kwargs = {k: v for k, v in conn_kwargs.items() if v is not None}

    conn = connect(**conn_kwargs)
    try:
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
    finally:
        conn.close()

    return _rows_to_dataframe(columns, rows)


def _load_sqlite(conn_config: dict, query: str, base_dir: str) -> pl.DataFrame:
    import sqlite3

    db_path = conn_config.get("database", conn_config.get("file", ":memory:"))
    if db_path != ":memory:":
        db_path = str(Path(base_dir) / db_path)

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(query)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
    finally:
        conn.close()

    return _rows_to_dataframe(columns, rows)


def _load_postgresql(conn_config: dict, query: str) -> pl.DataFrame:
    try:
        import psycopg2
    except ImportError as err:
        raise ImportError(
            "PostgreSQL driver not installed. Run: pip install psycopg2-binary"
        ) from err

    conn_kwargs = {
        "host": conn_config.get("host", "localhost"),
        "port": int(conn_config.get("port", 5432)),
        "dbname": conn_config.get("database", conn_config.get("dbname")),
        "user": conn_config.get("user"),
        "password": conn_config.get("password"),
    }
    conn_kwargs = {k: v for k, v in conn_kwargs.items() if v is not None}

    conn = psycopg2.connect(**conn_kwargs)
    try:
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
    finally:
        conn.close()

    return _rows_to_dataframe(columns, rows)


def _resolve_json_path(data: dict, path: str):
    """Extract a value from nested JSON using a dot/bracket path."""
    import re
    current = data
    # Split on dots but handle bracket notation
    tokens = re.split(r'\.|(?=\[)', path)
    tokens = [t for t in tokens if t]
    for token in tokens:
        # Handle bracket indexing like [0] or key[0]
        bracket_match = re.match(r'^([^\[]*)?\[(\d+)\]$', token)
        if bracket_match:
            key, idx = bracket_match.group(1), int(bracket_match.group(2))
            if key:
                current = current[key]
            current = current[idx]
        else:
            current = current[token]
    return current


def _resolve_url_from(url_from_config: dict, verify, timeout: float) -> str:
    """Resolve a download URL by fetching a JSON metadata endpoint."""
    try:
        import httpx
    except ImportError as err:
        raise ImportError("HTTP loader requires httpx. Run: pip install httpx") from err

    endpoint = url_from_config.get("endpoint", "").strip()
    json_path = url_from_config.get("json_path", "").strip()
    headers = url_from_config.get("headers", {})

    if not endpoint:
        raise ValueError("url_from requires an 'endpoint' field")
    if not json_path:
        raise ValueError("url_from requires a 'json_path' field")

    logger.info("Resolving download URL from %s", endpoint)
    with httpx.Client(verify=verify, timeout=timeout, follow_redirects=True) as client:
        resp = client.get(endpoint, headers=headers)
        resp.raise_for_status()

    data = resp.json()
    try:
        resolved = _resolve_json_path(data, json_path)
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(
            f"Could not extract URL from {endpoint} at path '{json_path}': {e}"
        ) from e

    if not isinstance(resolved, str) or not resolved.startswith("http"):
        raise ValueError(
            f"Resolved value is not a valid URL: {resolved!r} "
            f"(from {endpoint} at path '{json_path}')"
        )

    logger.info("Resolved URL: %s", resolved)
    return resolved


def load_http(source_config: dict, base_dir: str = ".",
              recipe_name: str = "", source_name: str = "") -> pl.DataFrame:
    """Download a file from a URL (or resolve via url_from) and load as DataFrame."""
    config = _interpolate_dict(source_config)
    url = config.get("url", "").strip()
    url_from = config.get("url_from")

    if not url and not url_from:
        raise ValueError("HTTP loader requires a 'url' or 'url_from' field")

    if url_from and not url:
        verify = config.get("verify", True)
        timeout = float(config.get("timeout", 300))
        if isinstance(verify, str):
            if verify.lower() in ("false", "0", "no"):
                verify = False
            elif verify.lower() in ("true", "1", "yes"):
                verify = True
        url = _resolve_url_from(url_from, verify, timeout)
        config["url"] = url

    cached = _read_cache(config, base_dir, recipe_name, source_name)
    if cached is not None:
        columns = config.get("columns")
        if columns:
            cached = cached.select(columns)
        return cached

    df = _fetch_and_parse(config, base_dir)

    _write_cache(df, config, base_dir, recipe_name, source_name)

    columns = config.get("columns")
    if columns:
        df = df.select(columns)

    return df


def _fetch_and_parse(config: dict, base_dir: str) -> pl.DataFrame:
    """Download URL content and parse into a DataFrame."""
    import io

    try:
        import httpx
    except ImportError as err:
        raise ImportError("HTTP loader requires httpx. Run: pip install httpx") from err

    url = config["url"]
    fmt = config.get("format", "").lower()
    zip_entry = config.get("zip_entry")
    headers = config.get("headers", {})
    verify = config.get("verify", True)
    timeout = float(config.get("timeout", 300))

    if isinstance(verify, str):
        if verify.lower() in ("false", "0", "no"):
            verify = False
        elif verify.lower() in ("true", "1", "yes"):
            verify = True

    logger.info("Downloading %s", url)
    print(f"    Downloading: {url.split('/')[-1][:60]}...", flush=True)
    with httpx.Client(verify=verify, timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()

    data = resp.content
    size_mb = len(data) / (1024 * 1024)
    print(f"    Downloaded: {size_mb:.1f} MB", flush=True)
    content_type = resp.headers.get("content-type", "")

    if not fmt:
        fmt = _detect_format(url, content_type)

    if fmt == "zip" or url.lower().endswith(".zip"):
        data, fmt = _extract_from_zip(data, zip_entry, fmt)

    if fmt in ("csv", "tsv"):
        separator = "\t" if fmt == "tsv" else ","
        df = pl.read_csv(io.BytesIO(data), infer_schema_length=0, separator=separator)
    elif fmt == "parquet":
        df = pl.read_parquet(io.BytesIO(data))
    elif fmt == "json" or fmt == "jsonl":
        df = pl.read_ndjson(io.BytesIO(data)) if fmt == "jsonl" else pl.read_json(io.BytesIO(data))
        df = df.cast({col: pl.String for col in df.columns})
    else:
        raise ValueError(
            f"Cannot determine format for URL: {url}. "
            f"Set 'format' explicitly (csv, tsv, parquet, json, jsonl, zip)."
        )

    if fmt == "parquet":
        df = df.cast({col: pl.String for col in df.columns})

    logger.info("Loaded %d rows x %d cols from %s", df.height, df.width, url)
    return df


def _detect_format(url: str, content_type: str) -> str:
    """Best-effort format detection from URL path or content-type."""
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()

    # Strip .gz/.zip suffix to find the inner format
    if path.endswith(".zip"):
        inner = path[:-4]
        if inner.endswith(".csv"):
            return "zip"
        return "zip"
    if path.endswith(".csv") or path.endswith(".csv.gz"):
        return "csv"
    if path.endswith(".tsv") or path.endswith(".tsv.gz"):
        return "tsv"
    if path.endswith(".parquet"):
        return "parquet"
    if path.endswith(".json"):
        return "json"
    if path.endswith(".jsonl") or path.endswith(".ndjson"):
        return "jsonl"

    # Fallback to content-type
    if "csv" in content_type:
        return "csv"
    if "parquet" in content_type:
        return "parquet"
    if "json" in content_type:
        return "json"

    return ""


def _extract_from_zip(data: bytes, zip_entry: str | None, outer_fmt: str) -> tuple[bytes, str]:
    """Extract a file from a ZIP archive, return (bytes, format)."""
    import io
    import zipfile

    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    # Filter out directories and __MACOSX junk
    data_files = [n for n in names if not n.endswith("/") and "__MACOSX" not in n]

    if not data_files:
        raise ValueError("ZIP archive is empty")

    if zip_entry:
        if zip_entry not in data_files:
            raise ValueError(
                f"zip_entry '{zip_entry}' not found in archive. "
                f"Available: {data_files[:10]}"
            )
        target = zip_entry
    elif len(data_files) == 1:
        target = data_files[0]
    else:
        # Pick the largest file as a heuristic
        target = max(data_files, key=lambda n: zf.getinfo(n).file_size)
        logger.info("ZIP has %d files, picking largest: %s", len(data_files), target)

    extracted = zf.read(target)

    # Detect inner format
    lower = target.lower()
    if lower.endswith(".csv"):
        inner_fmt = "csv"
    elif lower.endswith(".tsv"):
        inner_fmt = "tsv"
    elif lower.endswith(".parquet"):
        inner_fmt = "parquet"
    elif lower.endswith(".json"):
        inner_fmt = "json"
    elif lower.endswith(".jsonl") or lower.endswith(".ndjson"):
        inner_fmt = "jsonl"
    else:
        inner_fmt = "csv"

    return extracted, inner_fmt


LOADERS = {
    "file": load_file,
    "sql": load_sql,
    "http": load_http,
}


def dispatch_loader(source_config: dict, base_dir: str = ".",
                    recipe_name: str = "", source_name: str = "") -> pl.DataFrame:
    loader = source_config.get("loader")

    if loader is None and "file" in source_config:
        return load_file(source_config, base_dir)

    if loader is None:
        raise ValueError("Source config must have either 'file' or 'loader' key")

    loader_fn = LOADERS.get(loader)
    if loader_fn is None:
        raise ValueError(f"Unknown loader: {loader}. Available: {list(LOADERS.keys())}")

    return loader_fn(source_config, base_dir,
                     recipe_name=recipe_name, source_name=source_name)
