# YunOS v8.0-D2.1 Fixed Production Authority Boundary

Boundary version: `v8.0-d2.1-fixed-production-authority-boundary-1`.

This release narrows the supported production/test database boundary. The
formal validator in `memory/db_path_policy.py` rebuilds the eight code-defined
production database identities on every call. It does not accept a protected
set, policy object, production root, resolver, environment variable, or config
override. The v8.0-A, D1, and D2 profile test primitives use only this fixed
validator and the verified SQLite connection functions.

The protected identities are the main chat database, semantic sidecar,
approved-profile sidecar, runtime sidecar, memory sidecar, knowledge sidecar,
review ledger, and review authority registry. The publicly inspectable tuple is
an inventory for audit only; the formal validator does not read it as mutable
authority.

## Guaranteed boundary

For normal YunOS code, Codex automation, and supported test helpers:

- a caller-controlled test path cannot select a production database through a
  supported path, policy, root, resolver, environment, or config override;
- absolute/normcase/realpath checks, `samefile`, `st_dev`/`st_ino`, hardlink,
  symlink/junction/reparse, and parent-alias checks fail closed;
- a missing test database is checked at its parent, exclusively created, and
  verified again before schema initialization;
- an existing write target is snapshotted before SQLite `mode=rw` open, checked
  through `PRAGMA database_list`, and matched by file identity before writes;
- cleanup removes a failed created target only when it still has the identity
  created by that operation;
- production services continue to use fixed production paths and accept no
  caller path or policy override.

Tests simulate production identities only in test code and call the pure
single-target identity helper directly. Production modules do not import that
test harness, and tests do not install a replacement authority into the formal
validator.

## Explicitly out of scope

This v8 boundary does not claim to resist:

- arbitrary monkey patching or arbitrary code execution inside the same Python
  process, which could replace functions, standard-library calls, or SQLite;
- direct database modification by another program running as the same Windows
  user;
- source or data tampering by a local administrator.

These are host/process authority problems rather than supported API injection
paths. Documentation and audit output must stay within the supported-entrypoint
boundary and must not claim resistance to arbitrary same-process execution.

## v9 candidates

Stronger production authority should be evaluated as a separate version:

- a dedicated production writer process;
- a dedicated OS identity;
- NTFS ACLs on production databases and directories;
- a test account with read-only or no access to production data.

No R1, R2, or R3 recovery authorization or execution is part of D2.1.
