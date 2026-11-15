# Sanitized source snapshot

This snapshot includes current source code, tests, web assets and the Electron desktop launcher. It intentionally starts a new Git history.

- No production databases, attachments, conversation exports, private configuration, environment secrets, logs, backups, dependencies or packaged applications are included.
- Personal names and Windows user-directory identifiers are replaced with examples.
- Public project configuration and assistant persona are generic examples.
- Historical deployment/governance/acceptance records and production seed observations are replaced with placeholders; original files remain only in the local source checkout.
- Documentation fingerprint values are illustrative zeros. Do not use this snapshot's documentation as production recovery authorization.
- Security tests retain clearly synthetic credential sentinels and example addresses to test rejection/redaction behavior.
- Desktop launch requires a locally created config_private/primary_host.json matching the machine and project path, and the required local databases. Those resources are not distributed.

This public repository contains only the sanitized snapshot and its subsequent updates. The original private repository and its pre-existing branches/history are separate and have not been published or audited by this snapshot.

## Validation

Python compilation passed. The HTTP request hardening tests (9), prompt golden tests (2), and desktop host tests (2) passed. This is targeted validation, not the complete regression suite; tests requiring omitted historical deployment records or local databases are not claimed to pass. Credential-pattern and identity scans were reviewed; synthetic rejection-test strings are retained intentionally.
