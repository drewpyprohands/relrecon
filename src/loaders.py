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
            except ValueError:
                raise ValueError(f"Invalid cache_ttl: {ttl_str}")

    try:
        return float(s)
    except ValueError:
        raise ValueError(f"Invalid cache_ttl: {ttl_str}")


def _cache_key(source_config: dict) -> str:
    conn = dict(source_config.get("connection", {}))
    conn.pop("password", None)
    raw = json.dumps({
        "driver": source_config.get("driver", ""),
        "connection": conn,
        "query": source_config.get("query", ""),
    }, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def _get_cache_path(source_config: dict, base_dir: str,
                    recipe_name: str = "", source_name: str = "") -> Path:
    key = _cache_key(source_config)
    date_str = time.strftime("%Y%m%d")

    parts = []
    if recipe_name:
        parts.append(re.sub(r"[^a-z0-9]+", "_", recipe_name.lower()).strip("_"))
    if source_name:
        parts.append(re.sub(r"[^a-z0-9]+", "_", source_name.lower()).strip("_"))
    parts.append(date_str)
    parts.append(key)

    return Path(base_dir) / CACHE_DIR / ("_".join(parts) + ".parquet")


def _find_cached_file(source_config: dict, base_dir: str) -> Path | None:
    key = _cache_key(source_config)
    cache_dir = Path(base_dir) / CACHE_DIR
    if not cache_dir.exists():
        return None
    for f in cache_dir.glob(f"*_{key}.parquet"):
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
    return pl.read_parquet(str(cache_path))


def _write_cache(df: pl.DataFrame, source_config: dict, base_dir: str,
                 recipe_name: str = "", source_name: str = ""):
    ttl_seconds = _parse_ttl(source_config.get("cache_ttl", DEFAULT_CACHE_TTL))
    if ttl_seconds is None:
        return

    key = _cache_key(source_config)
    cache_dir = Path(base_dir) / CACHE_DIR
    if cache_dir.exists():
        for old in cache_dir.glob(f"*_{key}.parquet"):
            old.unlink(missing_ok=True)

    cache_path = _get_cache_path(source_config, base_dir, recipe_name, source_name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
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
    return df


def _load_trino(conn_config: dict, query: str) -> pl.DataFrame:
    try:
        from trino.dbapi import connect
        from trino.auth import BasicAuthentication
    except ImportError:
        raise ImportError("Trino driver not installed. Run: pip install trino")

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
    except ImportError:
        raise ImportError(
            "PostgreSQL driver not installed. Run: pip install psycopg2-binary"
        )

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


LOADERS = {
    "file": load_file,
    "sql": load_sql,
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
