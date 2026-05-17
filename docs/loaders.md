# Data Source Loaders

Pluggable data source loaders. Recipes can load from local files (default) or SQL databases.

## HTTP Loader

Download data directly from a URL. Supports CSV, TSV, Parquet, JSON, JSONL, and ZIP archives.

Requires: `pip install httpx`

### Basic CSV download

```yaml
sources:
  reference_data:
    loader: http
    url: "https://example.com/export.csv"
    columns: [id, name, category]
    cache_ttl: "7d"
    type: trusted_reference
```

### Dynamic URL resolution (url_from)

Many data providers publish a metadata API that returns the current download URL
(GLEIF, CKAN/data.gov, GitHub Releases, etc.). Use `url_from` to resolve the
latest URL automatically instead of hardcoding dated paths:

```yaml
sources:
  gleif_lei:
    loader: http
    url_from:
      endpoint: "https://leidata-preview.gleif.org/api/v2/golden-copies/publishes?page=1"
      json_path: "data[0].lei2.full_file.csv.url"
    format: zip
    columns: [LEI, Entity.LegalName, Entity.LegalAddress.Country]
    cache_ttl: "7d"
```

The loader GETs the `endpoint`, parses the JSON response, and extracts the
download URL using `json_path`. Supports dot notation and array indexing:

- `data[0].files.csv.url` -- GLEIF-style nested object
- `result.resources[0].url` -- CKAN/data.gov pattern
- `assets[0].browser_download_url` -- GitHub Releases
- `download_url` -- simple top-level field

`url_from` and `url` are mutually exclusive. If both are set, `url` takes priority.

### ZIP archive

```yaml
sources:
  gleif_lei:
    loader: http
    url_from:
      endpoint: "https://leidata-preview.gleif.org/api/v2/golden-copies/publishes?page=1"
      json_path: "data[0].lei2.full_file.csv.url"
    format: zip
    zip_entry: "lei_records.csv"  # optional: pick specific file in archive
    columns: [LEI, Entity.LegalName, Entity.LegalAddress.Country]
    cache_ttl: "7d"
    timeout: 600
```

If `zip_entry` is omitted: uses the single file if there's only one, otherwise picks the largest.

### Options

| Field | Default | Description |
|-------|---------|-------------|
| url | -- | Direct URL to download. Supports `${VAR}` env interpolation. |
| url_from | -- | Resolve URL from JSON API (see above). |
| url_from.endpoint | (required) | Metadata API URL. |
| url_from.json_path | (required) | Dot/bracket path to extract download URL. |
| url_from.headers | {} | HTTP headers for the metadata request. |
| format | auto | `csv`, `tsv`, `parquet`, `json`, `jsonl`, `zip`. Auto-detected from URL extension if omitted. |
| zip_entry | auto | Specific file to extract from ZIP. |
| columns | all | Subset of columns to keep after loading. |
| headers | {} | HTTP headers for the download request (e.g. API keys). |
| verify | true | SSL: `true`, `false`, or path to CA cert bundle. |
| timeout | 300 | Request timeout in seconds. |
| cache_ttl | 24h | Same caching as SQL loader (see below). |
| cache_format | parquet | `parquet` or `csv` for cache file format. |

Either `url` or `url_from` must be provided.

### Custom headers / auth

```yaml
sources:
  api_data:
    loader: http
    url: "https://api.vendor.com/v1/export.csv"
    headers:
      Authorization: "Bearer ${API_TOKEN}"
      Accept: "text/csv"
    cache_ttl: "12h"
```

---

## Example Templates

Real-world examples showing both direct URL and `url_from` patterns.

### GLEIF Golden Copy (url_from + ZIP)

GLEIF publishes daily LEI data. The download URL changes every day, but their
metadata API always returns the latest.

```yaml
# LEI entity records (~3.3M records, ~484 MB ZIP containing CSV)
sources:
  gleif_lei:
    loader: http
    url_from:
      endpoint: "https://leidata-preview.gleif.org/api/v2/golden-copies/publishes?page=1"
      json_path: "data[0].lei2.full_file.csv.url"
    format: zip
    columns:
      - LEI
      - Entity.LegalName
      - Entity.LegalAddress.FirstAddressLine
      - Entity.LegalAddress.City
      - Entity.LegalAddress.Country
      - Registration.RegistrationStatus
    cache_ttl: "7d"
    cache_format: csv       # 209 MB CSV, openable in Excel
    timeout: 300
```

```yaml
# Relationship records -- who owns whom (~473K records, ~24 MB ZIP)
sources:
  gleif_relationships:
    loader: http
    url_from:
      endpoint: "https://leidata-preview.gleif.org/api/v2/golden-copies/publishes?page=1"
      json_path: "data[0].rr.full_file.csv.url"
    format: zip
    columns:
      - Relationship.StartNode.NodeID
      - Relationship.EndNode.NodeID
      - Relationship.RelationshipType
      - Relationship.RelationshipStatus
    cache_ttl: "7d"
    timeout: 60
```

**API response shape:**
```json
{
  "data": [{
    "publish_date": "2026-05-16 16:00:00",
    "lei2": {
      "full_file": {
        "csv": { "url": "https://leidata-preview.gleif.org/storage/golden-copy-files/2026/05/16/...csv.zip" },
        "json": { "url": "..." },
        "xml": { "url": "..." }
      }
    },
    "rr": { "full_file": { "csv": { "url": "..." } } },
    "repex": { "full_file": { "csv": { "url": "..." } } }
  }]
}
```

---

### CKAN / data.gov (url_from)

CKAN powers data.gov, EU Open Data Portal, and hundreds of government open data
sites. They all share the same API pattern.

```yaml
# FDIC Failed Bank List from data.gov
sources:
  fdic_failed_banks:
    loader: http
    url_from:
      endpoint: "https://catalog.data.gov/api/3/action/package_show?id=fdic-failed-bank-list"
      json_path: "result.resources[0].url"
    cache_ttl: "30d"
```

```yaml
# Any CKAN portal -- just change the base URL
sources:
  uk_companies:
    loader: http
    url_from:
      endpoint: "https://data.gov.uk/api/3/action/package_show?id=uk-company-register"
      json_path: "result.resources[0].url"
    cache_ttl: "7d"
```

**API response shape:**
```json
{
  "success": true,
  "result": {
    "resources": [
      { "format": "CSV", "url": "https://data.gov/download/banks.csv" },
      { "format": "JSON", "url": "https://data.gov/download/banks.json" }
    ]
  }
}
```

---

### GitHub Releases (url_from)

Many open datasets are published as GitHub release assets. The "latest" release
API always points to the current version.

```yaml
# S&P 500 constituents from a dataset repo
sources:
  sp500:
    loader: http
    url_from:
      endpoint: "https://api.github.com/repos/datasets/s-and-p-500-companies/releases/latest"
      json_path: "assets[0].browser_download_url"
      headers:
        Accept: "application/vnd.github+json"
    cache_ttl: "7d"
```

```yaml
# GeoNames country info from a release
sources:
  geonames:
    loader: http
    url_from:
      endpoint: "https://api.github.com/repos/datasets/geonames/releases/latest"
      json_path: "assets[0].browser_download_url"
      headers:
        Authorization: "Bearer ${GITHUB_TOKEN}"  # optional, for rate limits
    cache_ttl: "30d"
```

**API response shape:**
```json
{
  "tag_name": "v2026.1",
  "assets": [
    {
      "name": "constituents.csv",
      "browser_download_url": "https://github.com/.../releases/download/v2026.1/constituents.csv"
    }
  ]
}
```

---

### ECB Statistical Data (direct URL)

The European Central Bank serves CSV directly at stable URLs -- no resolution
needed. Good example of the simple direct-download pattern.

```yaml
# EUR/USD monthly exchange rates
sources:
  ecb_eurusd:
    loader: http
    url: "https://data-api.ecb.europa.eu/service/data/EXR/M.USD.EUR.SP00.A?format=csvdata"
    columns: [TIME_PERIOD, OBS_VALUE, CURRENCY]
    cache_ttl: "1d"
```

```yaml
# All EUR exchange rates (daily, all currencies)
sources:
  ecb_all_rates:
    loader: http
    url: "https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A?format=csvdata"
    cache_ttl: "1d"
```

---

### World Bank Open Data (direct URL + ZIP)

World Bank's API returns a ZIP containing CSV when you add `downloadformat=csv`.

```yaml
# GDP by country (all years)
sources:
  world_bank_gdp:
    loader: http
    url: "https://api.worldbank.org/v2/country/all/indicator/NY.GDP.MKTP.CD?downloadformat=csv"
    format: zip
    cache_ttl: "30d"
    timeout: 60
```

---

### SEC EDGAR (direct URL with auth)

SEC requires a User-Agent header identifying the requester.

```yaml
# SEC company tickers
sources:
  sec_tickers:
    loader: http
    url: "https://www.sec.gov/files/company_tickers.json"
    format: json
    headers:
      User-Agent: "YourCompany admin@company.com"
    cache_ttl: "1d"
```

---

### Pattern Summary

| Provider | Pattern | Notes |
|----------|---------|-------|
| GLEIF | `url_from` | Daily publishes, CSV inside ZIP |
| CKAN / data.gov | `url_from` | `resources[N].url` pattern |
| GitHub Releases | `url_from` | `assets[N].browser_download_url` |
| ECB | direct `url` | Stable URL, format via query param |
| World Bank | direct `url` | ZIP download via `downloadformat=csv` |
| SEC EDGAR | direct `url` | Requires User-Agent header |
| Any vendor API | direct `url` + `headers` | Bearer token auth |

---

## File Loader (default)

```yaml
sources:
  vendor_export:
    file: vendor_data.csv
    type: multi_population
```

Supported formats: CSV, TSV, Parquet. Optional `columns` field selects a subset at load time.

## SQL Loader

Load directly from a database. Drivers are lazy-imported -- no errors unless a recipe actually uses one.

### Trino

```yaml
sources:
  warehouse:
    loader: sql
    driver: trino
    connection:
      host: ${TRINO_HOST}
      port: 8080
      user: ${TRINO_USER}
      catalog: hive
      schema: analytics
    query: |
      SELECT vendor_id, vendor_name, address_line1
      FROM vendor_master WHERE status = 'ACTIVE'
    type: multi_population
```

Requires: `pip install trino`

Optional fields: `password`, `http_scheme` (https), `verify` (SSL cert verification).

If `user` or `password` are in the config but unresolved (env var not set), the loader prompts interactively. Password uses hidden input.

### PostgreSQL

```yaml
sources:
  warehouse:
    loader: sql
    driver: postgresql
    connection:
      host: ${DB_HOST}
      port: 5432
      database: analytics
      user: ${DB_USER}
      password: ${DB_PASSWORD}
    query: SELECT vendor_id, vendor_name FROM vendor_master
    type: multi_population
```

Requires: `pip install psycopg2-binary`

### SQLite

```yaml
sources:
  local_db:
    loader: sql
    driver: sqlite
    connection:
      database: local_cache.db
    query: SELECT id, name FROM reference_data
```

No extra dependencies (stdlib).

### Drivers

| Driver | Package | Notes |
|--------|---------|-------|
| sqlite | (stdlib) | Local testing, cached datasets |
| postgresql | psycopg2-binary | Production databases |
| trino | trino | Data warehouses |
| http | httpx | Remote file download (CSV, ZIP, etc.) |

## Caching

SQL results are cached as Parquet files to avoid hitting the database on every run. Enabled by default (24h TTL).

```yaml
sources:
  warehouse:
    loader: sql
    driver: trino
    cache_ttl: "24h"     # default
    # cache_ttl: "12h"   # half a day
    # cache_ttl: "7d"    # a week
    # cache_ttl: "off"   # disable
    cache_format: csv    # default: parquet
```

Formats: `Nh` (hours), `Nm` (minutes), `Nd` (days), `N` (seconds), `off`.

### Cache format

| Format | Default | Notes |
|--------|---------|-------|
| parquet | yes | Compact, fast to read, preserves types |
| csv | | Excel-openable, shareable with non-technical users |

```yaml
cache_format: csv   # hand the cached file to someone with Excel
```

Applies to both SQL and HTTP loaders.

Cache files stored in `data/.cache/` (git-ignored). Delete the directory to force a fresh fetch. Filenames include recipe name, source name, date and a hash:

```
tpch_parts_catalog_reconciliation_migrated_parts_20260514_a1b2c3d4.parquet
```

## Environment Variables

Connection config supports `${VAR_NAME}` interpolation:

```yaml
connection:
  host: ${PGHOST}
  password: ${PGPASSWORD}
```

Unresolved vars stay as-is, which will cause a connection error -- making missing env vars obvious.

## Behavior

- All values cast to String (consistent with CSV loading)
- SQL NULL becomes Polars null (works with is_not_null/is_null filters)
- Existing recipes without `loader` key work unchanged (backward compatible)
- Password excluded from cache key (never appears in filenames)

## Adding New Drivers

1. Add `_load_<driver>()` in `src/loaders.py`
2. Register in the driver dispatch within `load_sql()`
3. Add tests (use SQLite patterns as template)
4. Update `config/recipe_schema.json`
