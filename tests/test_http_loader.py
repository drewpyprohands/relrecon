"""Tests for the HTTP download loader."""

import io
import os
import sys
import zipfile

import httpx
import pytest
import respx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from loaders import (
    dispatch_loader,
    _detect_format,
    _extract_from_zip,
    _cache_ext,
    _resolve_json_path,
)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def test_detect_format_csv_url():
    assert _detect_format("https://example.com/data.csv", "") == "csv"


def test_detect_format_csv_gz_url():
    assert _detect_format("https://example.com/data.csv.gz", "") == "csv"


def test_detect_format_tsv_url():
    assert _detect_format("https://example.com/export.tsv", "") == "tsv"


def test_detect_format_parquet_url():
    assert _detect_format("https://example.com/warehouse.parquet", "") == "parquet"


def test_detect_format_json_url():
    assert _detect_format("https://example.com/api/data.json", "") == "json"


def test_detect_format_jsonl_url():
    assert _detect_format("https://example.com/stream.jsonl", "") == "jsonl"


def test_detect_format_zip_url():
    assert _detect_format("https://example.com/archive.zip", "") == "zip"


def test_detect_format_from_content_type():
    assert _detect_format("https://example.com/download", "text/csv") == "csv"
    assert _detect_format("https://example.com/download", "application/json") == "json"


def test_detect_format_unknown_returns_empty():
    assert _detect_format("https://example.com/blob", "application/octet-stream") == ""


# ---------------------------------------------------------------------------
# ZIP extraction
# ---------------------------------------------------------------------------


def test_extract_from_zip_single_file():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.csv", "id,name\n1,Alice\n")
    data, fmt = _extract_from_zip(buf.getvalue(), None, "zip")
    assert fmt == "csv"
    assert b"Alice" in data


def test_extract_from_zip_named_entry():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "ignore me")
        zf.writestr("lei_records.csv", "LEI,Name\nL001,Corp\n")
    data, fmt = _extract_from_zip(buf.getvalue(), "lei_records.csv", "zip")
    assert fmt == "csv"
    assert b"Corp" in data


def test_extract_from_zip_missing_entry():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("other.csv", "x\n1\n")
    with pytest.raises(ValueError, match="not found in archive"):
        _extract_from_zip(buf.getvalue(), "missing.csv", "zip")


def test_extract_from_zip_empty_archive():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w"):
        pass
    with pytest.raises(ValueError, match="empty"):
        _extract_from_zip(buf.getvalue(), None, "zip")


def test_extract_from_zip_picks_largest():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("small.txt", "hi")
        zf.writestr("big.csv", "id,name\n" + "1,data\n" * 100)
    data, fmt = _extract_from_zip(buf.getvalue(), None, "zip")
    assert fmt == "csv"
    assert len(data) > 10


def test_extract_from_zip_ignores_macosx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("__MACOSX/._data.csv", "junk")
        zf.writestr("data.csv", "id\n1\n")
    data, fmt = _extract_from_zip(buf.getvalue(), None, "zip")
    assert fmt == "csv"
    assert b"junk" not in data


# ---------------------------------------------------------------------------
# cache_format helper
# ---------------------------------------------------------------------------


def test_cache_ext_default():
    assert _cache_ext({}) == ".parquet"
    assert _cache_ext({"cache_format": "parquet"}) == ".parquet"


def test_cache_ext_csv():
    assert _cache_ext({"cache_format": "csv"}) == ".csv"
    assert _cache_ext({"cache_format": "CSV"}) == ".csv"


# ---------------------------------------------------------------------------
# HTTP loader integration (respx mocked)
# ---------------------------------------------------------------------------


@respx.mock
def test_http_loader_csv(tmp_path):
    respx.get("https://example.com/data.csv").respond(
        content=b"id,name,city\n1,Alice,NYC\n2,Bob,LA\n",
        content_type="text/csv",
    )
    config = {
        "loader": "http",
        "url": "https://example.com/data.csv",
        "cache_ttl": "off",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 2
    assert df.columns == ["id", "name", "city"]
    assert df["name"][0] == "Alice"


@respx.mock
def test_http_loader_with_columns(tmp_path):
    respx.get("https://example.com/data.csv").respond(
        content=b"id,name,city,extra\n1,Alice,NYC,x\n2,Bob,LA,y\n",
        content_type="text/csv",
    )
    config = {
        "loader": "http",
        "url": "https://example.com/data.csv",
        "columns": ["id", "name"],
        "cache_ttl": "off",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.columns == ["id", "name"]
    assert df.height == 2


@respx.mock
def test_http_loader_zip_csv(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("lei_data.csv", "LEI,Name\nL001,Acme\nL002,Widget\n")

    respx.get("https://example.com/golden-copy.zip").respond(
        content=buf.getvalue(),
        content_type="application/zip",
    )
    config = {
        "loader": "http",
        "url": "https://example.com/golden-copy.zip",
        "format": "zip",
        "zip_entry": "lei_data.csv",
        "cache_ttl": "off",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 2
    assert "LEI" in df.columns
    assert df["LEI"][0] == "L001"


@respx.mock
def test_http_loader_caching_parquet(tmp_path):
    route = respx.get("https://example.com/data.csv").respond(
        content=b"id,val\n1,first\n",
        content_type="text/csv",
    )
    config = {
        "loader": "http",
        "url": "https://example.com/data.csv",
        "cache_ttl": "1h",
    }

    df1 = dispatch_loader(config, str(tmp_path))
    assert route.call_count == 1
    assert df1.height == 1

    # Second call should use cache -- no additional HTTP call
    df2 = dispatch_loader(config, str(tmp_path))
    assert route.call_count == 1
    assert df2.height == 1

    # Verify cache is parquet by default
    cache_dir = tmp_path / ".cache"
    assert len(list(cache_dir.glob("*.parquet"))) == 1
    assert len(list(cache_dir.glob("*.csv"))) == 0


@respx.mock
def test_http_loader_caching_csv_format(tmp_path):
    route = respx.get("https://example.com/data.csv").respond(
        content=b"id,val\n1,first\n2,second\n",
        content_type="text/csv",
    )
    config = {
        "loader": "http",
        "url": "https://example.com/data.csv",
        "cache_ttl": "1h",
        "cache_format": "csv",
    }

    df1 = dispatch_loader(config, str(tmp_path))
    assert route.call_count == 1
    assert df1.height == 2

    # Verify cache is CSV
    cache_dir = tmp_path / ".cache"
    csv_files = list(cache_dir.glob("*.csv"))
    assert len(csv_files) == 1
    assert len(list(cache_dir.glob("*.parquet"))) == 0

    # Readable in Excel -- just a plain CSV
    content = csv_files[0].read_text()
    assert "id,val" in content
    assert "first" in content

    # Second call uses CSV cache
    df2 = dispatch_loader(config, str(tmp_path))
    assert route.call_count == 1
    assert df2.height == 2


@respx.mock
def test_http_loader_env_interpolation(tmp_path):
    os.environ["TEST_GLEIF_HOST"] = "data.gleif.org"
    route = respx.get("https://data.gleif.org/api/download.csv").respond(
        content=b"x\n1\n",
        content_type="text/csv",
    )
    config = {
        "loader": "http",
        "url": "https://${TEST_GLEIF_HOST}/api/download.csv",
        "cache_ttl": "off",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 1
    assert route.called
    del os.environ["TEST_GLEIF_HOST"]


def test_http_loader_missing_url_raises():
    config = {"loader": "http"}
    with pytest.raises(ValueError, match="url"):
        dispatch_loader(config)


def test_http_loader_import_error(monkeypatch):
    _real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

    def _block_httpx(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError("No module named 'httpx'")
        return _real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block_httpx)
    for mod in list(sys.modules.keys()):
        if mod.startswith("httpx"):
            monkeypatch.delitem(sys.modules, mod)

    config = {
        "loader": "http",
        "url": "https://example.com/data.csv",
        "cache_ttl": "off",
    }
    with pytest.raises(ImportError, match="httpx"):
        dispatch_loader(config)


@respx.mock
def test_http_loader_unknown_format_raises(tmp_path):
    respx.get("https://example.com/blob").respond(
        content=b"binary junk",
        content_type="application/octet-stream",
    )
    config = {
        "loader": "http",
        "url": "https://example.com/blob",
        "cache_ttl": "off",
    }
    with pytest.raises(ValueError, match="Cannot determine format"):
        dispatch_loader(config, str(tmp_path))


@respx.mock
def test_http_loader_custom_headers(tmp_path):
    route = respx.get("https://api.example.com/export.csv").respond(
        content=b"id\n1\n",
        content_type="text/csv",
    )
    config = {
        "loader": "http",
        "url": "https://api.example.com/export.csv",
        "headers": {"Authorization": "Bearer secret123"},
        "cache_ttl": "off",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 1
    assert route.calls[0].request.headers["Authorization"] == "Bearer secret123"


@respx.mock
def test_http_loader_http_error_raises(tmp_path):
    respx.get("https://example.com/missing.csv").respond(status_code=404)
    config = {
        "loader": "http",
        "url": "https://example.com/missing.csv",
        "cache_ttl": "off",
    }
    with pytest.raises(httpx.HTTPStatusError):
        dispatch_loader(config, str(tmp_path))


# ---------------------------------------------------------------------------
# SQL loader cache_format (verify it works there too)
# ---------------------------------------------------------------------------


def test_sql_cache_format_csv(tmp_path):
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
        "cache_format": "csv",
    }

    df = dispatch_loader(config, base_dir=str(tmp_path))
    assert df.height == 2

    cache_dir = tmp_path / ".cache"
    csv_files = list(cache_dir.glob("*.csv"))
    assert len(csv_files) == 1
    assert "Alice" in csv_files[0].read_text()

    # Re-read from CSV cache
    df2 = dispatch_loader(config, base_dir=str(tmp_path))
    assert df2.height == 2


# ---------------------------------------------------------------------------
# url_from / JSON path resolution
# ---------------------------------------------------------------------------


def test_resolve_json_path_simple_key():
    assert _resolve_json_path({"url": "https://x.com/f.csv"}, "url") == "https://x.com/f.csv"


def test_resolve_json_path_nested_dot():
    data = {"result": {"resources": [{"url": "https://data.gov/file.csv"}]}}
    assert _resolve_json_path(data, "result.resources[0].url") == "https://data.gov/file.csv"


def test_resolve_json_path_gleif_style():
    data = {"data": [{"lei2": {"full_file": {"csv": {"url": "https://gleif.org/lei.zip"}}}}]}
    assert _resolve_json_path(data, "data[0].lei2.full_file.csv.url") == "https://gleif.org/lei.zip"


def test_resolve_json_path_missing_key():
    with pytest.raises(KeyError):
        _resolve_json_path({"a": 1}, "b")


def test_resolve_json_path_index_out_of_range():
    with pytest.raises(IndexError):
        _resolve_json_path({"data": []}, "data[0]")


@respx.mock
def test_url_from_resolves_and_downloads(tmp_path):
    """url_from hits metadata API, extracts URL, then downloads the file."""
    metadata = {
        "datasets": [{
            "files": {"csv": {"url": "https://cdn.example.com/latest.csv"}}
        }]
    }
    csv_data = "id,name\n1,Alpha\n2,Beta\n"

    respx.get("https://api.example.com/datasets/latest").mock(
        return_value=httpx.Response(200, json=metadata)
    )
    respx.get("https://cdn.example.com/latest.csv").mock(
        return_value=httpx.Response(200, text=csv_data,
                                   headers={"content-type": "text/csv"})
    )

    config = {
        "loader": "http",
        "url_from": {
            "endpoint": "https://api.example.com/datasets/latest",
            "json_path": "datasets[0].files.csv.url",
        },
        "cache_ttl": "off",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 2
    assert df.columns == ["id", "name"]
    assert df["name"][0] == "Alpha"


@respx.mock
def test_url_from_with_zip(tmp_path):
    """url_from resolves a URL that points to a ZIP containing CSV."""
    metadata = {"latest": {"download_url": "https://cdn.example.com/data.zip"}}

    # Build ZIP with CSV inside
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("records.csv", "LEI,Name\nL001,Acme\nL002,Corp\n")
    zip_bytes = zip_buf.getvalue()

    respx.get("https://api.example.com/publish").mock(
        return_value=httpx.Response(200, json=metadata)
    )
    respx.get("https://cdn.example.com/data.zip").mock(
        return_value=httpx.Response(200, content=zip_bytes,
                                   headers={"content-type": "application/zip"})
    )

    config = {
        "loader": "http",
        "url_from": {
            "endpoint": "https://api.example.com/publish",
            "json_path": "latest.download_url",
        },
        "format": "zip",
        "cache_ttl": "off",
    }
    df = dispatch_loader(config, str(tmp_path))
    assert df.height == 2
    assert "LEI" in df.columns


@respx.mock
def test_url_from_bad_path_raises(tmp_path):
    """url_from with invalid json_path raises ValueError."""
    metadata = {"data": []}
    respx.get("https://api.example.com/empty").mock(
        return_value=httpx.Response(200, json=metadata)
    )

    config = {
        "loader": "http",
        "url_from": {
            "endpoint": "https://api.example.com/empty",
            "json_path": "data[0].url",
        },
        "cache_ttl": "off",
    }
    with pytest.raises(ValueError, match="Could not extract URL"):
        dispatch_loader(config, str(tmp_path))


def test_url_from_missing_endpoint():
    config = {
        "loader": "http",
        "url_from": {"json_path": "data.url"},
        "cache_ttl": "off",
    }
    with pytest.raises(ValueError, match="endpoint"):
        dispatch_loader(config, ".")


def test_url_from_missing_json_path():
    config = {
        "loader": "http",
        "url_from": {"endpoint": "https://api.example.com/x"},
        "cache_ttl": "off",
    }
    with pytest.raises(ValueError, match="json_path"):
        dispatch_loader(config, ".")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_dispatch_unknown_still_raises():
    with pytest.raises(ValueError, match="Unknown loader"):
        dispatch_loader({"loader": "ftp"})
