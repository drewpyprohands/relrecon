# Trino Quickstart

Local Trino setup for testing the SQL loader.

## Quick Setup

```bash
cd examples/trino
./setup.sh
```

This starts a Trino container on port 8085 with password auth and loads two sample tables:

| Table | Rows | Description |
|-------|------|-------------|
| `sample.demo.migrated_parts` | 15 | Mangled names (uppercased, word-reversed) |
| `sample.demo.trusted_parts` | 15 | Clean reference data |

Plus the built-in `tpch` benchmark data (customers, orders, parts, suppliers).

Default credentials: `test` / `test123`

## Run Recipes

```bash
# Small sample (15 rows, user/password auth)
export TRINO_HOST=localhost TRINO_USER=test TRINO_PASSWORD=test123
python3 -m src --recipe examples/trino/sample_recipe.yaml

# Larger tpch dataset (386 rows, no auth needed on plain container)
export TRINO_HOST=localhost
python3 -m src --recipe config/recipes/trino_tpch_demo.yaml
```

## Manual Setup (no auth)

```bash
docker run -d --name trino -p 8085:8080 trinodb/trino:latest
# Wait ~10s, then:
docker exec trino trino --execute "SELECT 'ready'"
```

## TPCH Built-in Data

No loading needed -- Trino generates this on the fly:

| Table | Rows (tiny) |
|-------|-------------|
| `tpch.tiny.customer` | 1500 |
| `tpch.tiny.orders` | 15000 |
| `tpch.tiny.part` | 2000 |
| `tpch.tiny.supplier` | 100 |

## Trino CLI

```bash
docker exec -it trino trino
docker exec trino trino --execute "SELECT count(*) FROM tpch.tiny.customer"
docker exec trino trino --catalog tpch --schema tiny --execute "SHOW TABLES"
```

## Cleanup

```bash
docker rm -f trino-test  # auth container from setup.sh
docker rm -f trino       # plain container
```

## SSL: Internal CA Troubleshooting

If your Trino endpoint uses an internal CA and Python throws
`self-signed certificate in certificate chain`, the CA chain
needs to be added to your Python cert bundle.

### Extract the cert chain

```bash
openssl s_client -connect trino.example.com:443 -showcerts </dev/null 2>/dev/null | \
  awk '/BEGIN CERTIFICATE/,/END CERTIFICATE/{print}' > /tmp/trino-chain.pem

# Verify it captured certs (expect 2-4)
grep -c 'BEGIN CERTIFICATE' /tmp/trino-chain.pem
```

### Option A: Append to certifi bundle (quick)

```bash
cat /tmp/trino-chain.pem >> "$(python3 -c 'import certifi; print(certifi.where())')"
```

Note: gets overwritten on `pip install --upgrade certifi`.

### Option B: Combined bundle (survives upgrades)

```bash
cp "$(python3 -c 'import certifi; print(certifi.where())')" ~/combined-ca.pem
cat /tmp/trino-chain.pem >> ~/combined-ca.pem
export SSL_CERT_FILE=~/combined-ca.pem
```

### Verify

```bash
python3 -c "
import ssl, socket
ctx = ssl.create_default_context()
with ctx.wrap_socket(socket.socket(), server_hostname='trino.example.com') as s:
    s.connect(('trino.example.com', 443))
    print('OK:', s.getpeercert()['subject'])
"
```

## Notes

- Password auth requires HTTPS (Trino enforces this)
- Default container (no setup.sh) has no auth -- `user` is just a label
- `verify: false` in connection config skips SSL cert verification (self-signed)
- The `memory` connector (sample data) is wiped on container restart
- The `tpch` connector generates data on the fly (no disk storage)
