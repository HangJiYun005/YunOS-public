# YunOS v5.1 Memory Change Detection

v5.1 is a read-only change-detection stage. It discovers source changes and
missing or stale coverage for a later controlled refresh; it does not generate
summaries, modify `memory_summaries`, update embeddings, call APIs/LLMs, or
connect to the Web.

## Source snapshot

`memory/source_snapshot.py` reads `conversations` and `messages` through
SQLite `mode=ro` connections and closes every connection explicitly. Each
conversation snapshot records its `memory_space`, message count, first and
last message timestamps, latest message ID, deterministic SHA-256 content hash,
and snapshot schema version. The hash canonically sorts message ID, role, and
content, so database row order does not affect it while content, role, insert,
or delete changes do.

## Detection states

The read-only CLI (`python tools/check_memory_changes.py --json`) reports:

- `summary_missing`
- `new_messages`
- `source_changed`
- `coverage_unknown`
- `orphaned_summary`
- `up_to_date`

It recommends `none`, `create_summary_candidate`,
`refresh_summary_candidate`, or `manual_review`; it never executes those
actions. Legacy summaries without reliable source count and hash coverage are
classified as `coverage_unknown`, never guessed as current.

## v5.1 acceptance snapshot

The current real read-only check found 177 conversations without summaries and
1 conversation with unknown coverage. The next v5.2 stage may generate summary
candidates only; it must not directly write production summaries. Messages are
allowed to be read for detection, while the real chat database and production
sidecar remain write-protected.
