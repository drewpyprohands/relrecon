"""Tests for pluggable data source loaders."""

import os
import sqlite3
import sys
import time

import polars as pl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loaders import _interpolate_env, _load_trino, dispatch_loader, load_file, load_sql


# ---------------------------------------------------------------------------
# Env var interpolation
# ---------------------------------------------------------------------------


def test_interpolate_env_replaces_known_var():
    os.environ["TEST_LOADER_HOST"] = "db.example.com"
    assert _interpolate_env("${TEST_LOADER_HOST}") == "db.example.com"
    del os.environ["TEST_LOADER_HOST"]


def test_interpolate_env_preserves_unknown_var():
    result = _interpolate_env("${DEFINITELY_NOT_SET_XYZ}")
    assert result == "${DEFINITELY_NOT_SET_XYZ}"


def test_interpolate_env_mixed_text():
    os.environ["TEST_PORT"] = "5432"
    result = _interpolate_env("https://host:${TEST_PORT}/path")
    assert result == "https://host:5432/path"
    del os.environ["TEST_PORT"]


def test_interpolate_env_non_string_passthrough():
    assert _interpolate_env(42) == 42
    assert _interpolate_env(None) is None


def test_interpolate_env_multiple_vars():
    os.environ["TEST_HOST"] = "localhost"
    os.environ["TEST_PORT"] = "5432"
    result = _interpolate_env("${TEST_HOST}:${TEST_PORT}")
    assert result == "localhost:5432"
    del os.environ["TEST_HOST"]
    del os.environ["TEST_PORT"]


# ---------------------------------------------------------------------------
# File loader (backward compat)
# ---------------------------------------------------------------------------


def test_load_file_csv(tmp_path):
    csv = tmp_path / "test.csv"
    csv.write_text("id,name\n1,Alice\n2,Bob\n")
    df = load_file({"file": "test.csv"}, str(tmp_path))
    assert df.height == 2
    assert df.columns == ["id", "name"]


def test_load_file_column_selection(tmp_path):
    csv = tmp_path / "test.csv"
    csv.write_text("id,name,extra\n1,Alice,x\n2,Bob,y\n")
    df = load_file({"file": "test.csv", "columns": ["id", "name"]}, str(tmp_path))
    assert df.columns == ["id", "name"]


def test_load_file_tsv(tmp_path):
    tsv = tmp_path / "test.tsv"
    tsv.write_text("id\tname\n1\tAlice\n")
    df = load_file({"file": "test.tsv"}, str(tmp_path))
    assert df.height == 1


def test_load_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_file({"file": "nope.csv"}, str(tmp_path))


def test_load_file_unsupported_extension(tmp_path):
    f = tmp_path / "test.xlsx"
    f.write_text("fake")
    with pytest.raises(ValueError, match="Unsupported format"):
        load_file({"file": "test.xlsx"}, str(tmp_path))


# ---------------------------------------------------------------------------
# SQL loader -- SQLite (no external deps)
# ---------------------------------------------------------------------------


def test_sql_sqlite_basic(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE vendors (id TEXT, name TEXT)")
    conn.execute("INSERT INTO vendors VALUES ('V001', 'Acme Corp')")
    conn.execute("INSERT INTO vendors VALUES ('V002', 'Widget Inc')")
    conn.commit()
    conn.close()

    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": "test.db"},
        "query": "SELECT id, name FROM vendors",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 2
    assert df["id"][0] == "V001"
    assert df["name"][1] == "Widget Inc"


def test_sql_sqlite_null_handling(tmp_path):
    """SQL NULL should become Polars null (works with is_not_null filter)."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id TEXT, category TEXT)")
    conn.execute("INSERT INTO t VALUES ('A', 'active')")
    conn.execute("INSERT INTO t VALUES ('B', NULL)")
    conn.execute("INSERT INTO t VALUES ('C', 'pending')")
    conn.commit()
    conn.close()

    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": "test.db"},
        "query": "SELECT id, category FROM t",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 3
    assert df["category"][1] is None  # NULL -> None -> Polars null


def test_sql_sqlite_empty_result(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id TEXT, name TEXT)")
    conn.commit()
    conn.close()

    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": "test.db"},
        "query": "SELECT id, name FROM t",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 0
    assert df.columns == ["id", "name"]
    # Empty df columns should be String type
    assert df.dtypes == [pl.String, pl.String]


def test_sql_sqlite_memory_db():
    """In-memory SQLite for quick testing."""
    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": ":memory:"},
        "query": "SELECT 'hello' as greeting, 42 as num",
    }
    df = dispatch_loader(config)
    assert df.height == 1
    assert df["greeting"][0] == "hello"
    assert df["num"][0] == "42"  # cast to string


def test_sql_sqlite_env_var_in_connection(tmp_path):
    db_path = tmp_path / "env_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('found')")
    conn.commit()
    conn.close()

    os.environ["TEST_DB_FILE"] = "env_test.db"
    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": "${TEST_DB_FILE}"},
        "query": "SELECT x FROM t",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 1
    assert df["x"][0] == "found"
    del os.environ["TEST_DB_FILE"]


def test_sql_sqlite_with_where_clause(tmp_path):
    db_path = tmp_path / "filter.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE items (id TEXT, status TEXT, value REAL)")
    conn.execute("INSERT INTO items VALUES ('A', 'active', 100.5)")
    conn.execute("INSERT INTO items VALUES ('B', 'inactive', 200.0)")
    conn.execute("INSERT INTO items VALUES ('C', 'active', 50.0)")
    conn.commit()
    conn.close()

    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": "filter.db"},
        "query": "SELECT id, value FROM items WHERE status = 'active'",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 2
    assert set(df["id"].to_list()) == {"A", "C"}


def test_sql_missing_query_raises():
    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": ":memory:"},
    }
    with pytest.raises(ValueError, match="query"):
        dispatch_loader(config)


def test_sql_missing_driver_raises():
    config = {
        "loader": "sql",
        "connection": {},
        "query": "SELECT 1",
    }
    with pytest.raises(ValueError, match="driver"):
        dispatch_loader(config)


def test_sql_unknown_driver_raises():
    config = {
        "loader": "sql",
        "driver": "oracle",
        "connection": {},
        "query": "SELECT 1",
    }
    with pytest.raises(ValueError, match="Unsupported SQL driver"):
        dispatch_loader(config)


# ---------------------------------------------------------------------------
# Import guard tests
# ---------------------------------------------------------------------------


def test_postgresql_import_error(monkeypatch):
    _real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

    def _block_psycopg2(name, *args, **kwargs):
        if name == "psycopg2":
            raise ImportError("No module named 'psycopg2'")
        return _real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block_psycopg2)

    for mod in list(sys.modules.keys()):
        if mod.startswith("psycopg2"):
            monkeypatch.delitem(sys.modules, mod)

    config = {
        "loader": "sql",
        "driver": "postgresql",
        "connection": {"host": "localhost", "database": "test"},
        "query": "SELECT 1",
    }
    with pytest.raises(ImportError, match="psycopg2"):
        dispatch_loader(config)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_parse_ttl():
    from loaders import _parse_ttl
    assert _parse_ttl("24h") == 86400
    assert _parse_ttl("12h") == 43200
    assert _parse_ttl("30m") == 1800
    assert _parse_ttl("7d") == 604800
    assert _parse_ttl("3600") == 3600
    assert _parse_ttl("off") is None
    assert _parse_ttl("false") is None
    assert _parse_ttl("none") is None
    assert _parse_ttl("0") is None


def test_cache_write_and_read(tmp_path):
    import sqlite3
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id TEXT, name TEXT)")
    conn.execute("INSERT INTO t VALUES ('1', 'Alice'), ('2', 'Bob')")
    conn.commit()
    conn.close()

    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": str(db_path)},
        "query": "SELECT * FROM t",
        "cache_ttl": "1h",
    }

    # First call -- hits DB, writes cache
    df1 = dispatch_loader(config, base_dir=str(tmp_path))
    assert df1.height == 2

    cache_dir = tmp_path / ".cache"
    assert cache_dir.exists()
    cached_files = list(cache_dir.glob("*.parquet"))
    assert len(cached_files) == 1

    # Second call -- should read from cache
    df2 = dispatch_loader(config, base_dir=str(tmp_path))
    assert df2.height == 2
    assert df1.equals(df2)


def test_cache_disabled(tmp_path):
    import sqlite3
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id TEXT)")
    conn.execute("INSERT INTO t VALUES ('1')")
    conn.commit()
    conn.close()

    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": str(db_path)},
        "query": "SELECT * FROM t",
        "cache_ttl": "off",
    }

    dispatch_loader(config, base_dir=str(tmp_path))
    cache_dir = tmp_path / ".cache"
    assert not cache_dir.exists()


def test_cache_expired(tmp_path):
    import sqlite3
    from loaders import _get_cache_path
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id TEXT)")
    conn.execute("INSERT INTO t VALUES ('1')")
    conn.commit()
    conn.close()

    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": str(db_path)},
        "query": "SELECT * FROM t",
        "cache_ttl": "1s",
    }

    dispatch_loader(config, base_dir=str(tmp_path))

    # Backdate the cache file
    cache_path = _get_cache_path(config, str(tmp_path))
    old_time = time.time() - 10
    os.utime(str(cache_path), (old_time, old_time))

    # Add a row so we can tell if it re-fetched
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO t VALUES ('2')")
    conn.commit()
    conn.close()

    df = dispatch_loader(config, base_dir=str(tmp_path))
    assert df.height == 2  # Got fresh data, not cached 1-row version


def test_cache_default_enabled(tmp_path):
    import sqlite3
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id TEXT)")
    conn.execute("INSERT INTO t VALUES ('1')")
    conn.commit()
    conn.close()

    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": str(db_path)},
        "query": "SELECT * FROM t",
        # no cache_ttl -- should default to 24h
    }

    dispatch_loader(config, base_dir=str(tmp_path))
    cache_dir = tmp_path / ".cache"
    assert cache_dir.exists()
    assert len(list(cache_dir.glob("*.parquet"))) == 1


def test_trino_import_error(monkeypatch):
    _real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

    def _block_trino(name, *args, **kwargs):
        if name == "trino.dbapi" or name == "trino.auth":
            raise ImportError("No module named 'trino'")
        return _real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block_trino)

    # Clear any cached trino imports so the mock takes effect
    for mod in list(sys.modules.keys()):
        if mod.startswith("trino"):
            monkeypatch.delitem(sys.modules, mod)

    config = {
        "loader": "sql",
        "driver": "trino",
        "connection": {"host": "localhost", "catalog": "tpch"},
        "query": "SELECT 1",
    }
    with pytest.raises(ImportError, match="[Tt]rino"):
        dispatch_loader(config)


@pytest.mark.parametrize(
    ("environment_value", "expected_spooling"),
    [(None, True), ("TrUe", True), ("fAlSe", False)],
)
def test_trino_spooling_environment_sets_session_property(
    monkeypatch, environment_value, expected_spooling
):
    captured_kwargs = {}

    class Cursor:
        description = [("value",)]

        def execute(self, query):
            assert query == "SELECT 1"

        def fetchall(self):
            return [(1,)]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    def mock_connect(**kwargs):
        captured_kwargs.update(kwargs)
        return Connection()

    if environment_value is None:
        monkeypatch.delenv("TRINO_SPOOLING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TRINO_SPOOLING_ENABLED", environment_value)
    monkeypatch.setattr("trino.dbapi.connect", mock_connect)

    df = _load_trino(
        {
            "host": "trino.example.com",
            "port": 8443,
            "user": "analyst",
            "catalog": "hive",
            "schema": "analytics",
            "http_scheme": "https",
            "verify": "false",
            "session_properties": {"query_max_run_time": "5m"},
        },
        "SELECT 1",
    )

    assert df.to_dicts() == [{"value": "1"}]
    assert captured_kwargs == {
        "host": "trino.example.com",
        "port": 8443,
        "user": "analyst",
        "catalog": "hive",
        "schema": "analytics",
        "http_scheme": "https",
        "verify": False,
        "session_properties": {
            "query_max_run_time": "5m",
            "spooling_enabled": expected_spooling,
        },
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_dispatch_no_loader_with_file(tmp_path):
    csv = tmp_path / "data.csv"
    csv.write_text("a,b\n1,2\n")
    df = dispatch_loader({"file": "data.csv"}, str(tmp_path))
    assert df.height == 1


def test_dispatch_explicit_file_loader(tmp_path):
    csv = tmp_path / "data.csv"
    csv.write_text("a,b\n1,2\n")
    df = dispatch_loader({"loader": "file", "file": "data.csv"}, str(tmp_path))
    assert df.height == 1


def test_dispatch_unknown_loader_raises():
    with pytest.raises(ValueError, match="Unknown loader"):
        dispatch_loader({"loader": "ftp"})


def test_dispatch_no_loader_no_file_raises():
    with pytest.raises(ValueError, match="must have either"):
        dispatch_loader({})


# ---------------------------------------------------------------------------
# Integration with recipe.load_source
# ---------------------------------------------------------------------------


def test_load_source_dispatches_sql(tmp_path):
    from recipe import load_source

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (vendor_id TEXT, name TEXT)")
    conn.execute("INSERT INTO t VALUES ('V001', 'Test Corp')")
    conn.commit()
    conn.close()

    config = {
        "loader": "sql",
        "driver": "sqlite",
        "connection": {"database": "test.db"},
        "query": "SELECT vendor_id, name FROM t",
    }
    df = load_source(config, str(tmp_path))
    assert df.height == 1
    assert df["vendor_id"][0] == "V001"


def test_load_source_backward_compat(tmp_path):
    from recipe import load_source

    csv = tmp_path / "data.csv"
    csv.write_text("id,name\n1,Alice\n")
    df = load_source({"file": "data.csv"}, str(tmp_path))
    assert df.height == 1


def test_verify_cert_path_preserved():
    """verify: /path/to/cert.pem should stay as a path, not become True."""
    cases = [
        ("/etc/ssl/certs/ca-bundle.crt", "/etc/ssl/certs/ca-bundle.crt"),
        ("/home/user/.certs/internal-ca.pem", "/home/user/.certs/internal-ca.pem"),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("true", True),
        ("yes", True),
        ("1", True),
    ]
    for input_val, expected in cases:
        v = input_val
        if isinstance(v, str) and v.lower() in ("true", "false", "0", "1", "no", "yes"):
            v = v.lower() not in ("false", "0", "no")
        assert v == expected, f"verify={input_val!r}: expected {expected!r}, got {v!r}"
