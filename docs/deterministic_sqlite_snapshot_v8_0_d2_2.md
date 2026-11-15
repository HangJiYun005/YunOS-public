# YunOS v8.0-D2.2 Deterministic SQLite Snapshot Encoding

Protocol: `v8.0-d2.2-deterministic-sqlite-snapshot-1`

This additive protocol supplies the read-only preflight/postflight snapshot
needed by production profile recovery. It does not change the sealed D2
authorization manifest, capability, B1/B2 transaction, or production schema.

## SQLite value encoding

Every cell binds the storage class returned by SQLite `typeof()`:

- `NULL` records an explicit null value;
- `INTEGER` records a signed 64-bit canonical decimal string;
- `REAL` records the exact IEEE-754 binary64 bytes as lowercase hexadecimal and
  rejects NaN or infinity;
- `TEXT` records strict UTF-8 base64, UTF-8 SHA-256, and exact Unicode code
  points without normalization or replacement fallback;
- `BLOB` records base64, SHA-256, and byte length. `bytes`, `bytearray`, and
  `memoryview` resolve to the same byte content.

Canonical serialization is sorted, compact, ASCII-only JSON. Empty TEXT and
empty BLOB, or TEXT `abc` and BLOB `0x616263`, remain distinct because their
storage classes and value fields differ.

## Rows, tables, and database identity

Table and column names come from `sqlite_schema` and `PRAGMA table_xinfo`, then
must pass the strict ASCII identifier policy before interpolation. The snapshot
binds table name, exact column order and metadata, declared schema SQL, actual
cell storage classes, SQLite schema/user/application versions, and encoding.

Rows are sorted in Python by canonical primary-key values. A table without a
declared primary key uses an unshadowed SQLite rowid; if neither a primary key
nor a reliable rowid can be established, snapshot creation fails closed. Table
names are sorted independently of database return order. Row, table, schema,
and database identities are SHA-256 digests of canonical bytes.

`verify_preexisting_rows_unchanged()` compares stable row ordering keys and row
identities. New rows may be present after an authorized append, but every row
captured before the operation must still exist with the same schema and exact
cell bytes.

## Production boundary

`snapshot_production_profile_authorization_database()` accepts no path, policy,
capability, credential, or dependency input. It opens only the fixed production
profile sidecar with SQLite `mode=ro&immutable=1`, verifies the actual opened
file identity, and snapshots the fixed D1/D2 table set. A pending journal/WAL,
unverifiable path, unsupported identifier, hidden column, non-finite REAL, or
unstable ordering fails before any production authorization or write boundary.

The privacy-safe CLI prints identities and counts only:

```powershell
python tools/profile_authorization_snapshot.py
```

Full snapshots are intended to remain in process for pre/post comparison. They
contain encoded database values and must not be logged, committed, returned to
the frontend, or copied into an authorization manifest.
