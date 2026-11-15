# YunOS v7.0 Memory Package Contract

## Boundary and entry points

The canonical protocol is `v7-memory-package-1`. It converts the sealed
`v6.4-retrieval-2` result into a model-independent context package. It does not
summarize, classify, resolve conflicts, deduplicate semantically, select by
tokens, persist a package, initialize a sidecar, or send content to a model.

The shared core is `MemoryPackageService.build(...)`. The fixed-path CLI and
GET preview are:

```powershell
python tools/memory_package_preview.py "search terms" --json
python tools/memory_package_preview.py "search terms" --start-time 2025-01-01 --future-token-budget 4096
```

```text
GET /api/memory-package-preview?q=search%20terms&format=json
GET /api/memory-package-preview?q=search%20terms&format=text
```

Neither public entry accepts a database path. The CLI uses the fixed production
paths. The GET endpoint is available only when the Web process is both
production mode and read-only and its resolved database is the one fixed
production main database; test mode, writable mode, and `--real` with any other
database return `production_readonly_required`. The endpoint calls the fixed
production builder instead of inheriting the Web launcher's `--db` value.

The core opens the source through v6.4 SQLite `mode=ro`. It then calls
`validate_memory_package_source_backed(..., trusted_source_db=...)`, which uses
a second `mode=ro`/`query_only` snapshot to verify every returned message anchor
and rebuild each database-backed public projection. The production semantic,
knowledge, memory, and review files are never initialized by this flow.

## Canonical identity

Canonical JSON is UTF-8 JSON with sorted object keys, no insignificant
whitespace, Unicode preserved, and non-finite numbers rejected. Evidence is
ordered by result rank, hit-type order (`keyword`, `semantic`, `graph`), then
evidence identity. Warnings, missing capabilities, degradation reasons, and
classification references have deterministic ordering.

`package_identity` is `mp_` plus SHA-256 of the deterministic package core. A
third-party implementation reproduces it exactly as follows:

1. Deep-copy every top-level package field except `package_identity` and
   `size`.
2. Add the canonical internal top-level object `budget_policy` with exactly
   `future_token_budget = package.size.future_token_budget` and
   `token_selection_applied = package.size.token_selection_applied`.
3. Serialize that object with Unicode preserved, keys sorted recursively,
   separators `,` and `:` with no added whitespace, and non-finite numbers
   rejected.
4. Hash the UTF-8 bytes with SHA-256 and prefix the lowercase hex digest with
   `mp_`.

This includes the normalized query, time range, scope, all retrieval
parameters, future token-budget declaration, v6.4 protocol/status/availability,
privacy state, evidence, warnings, degradation, and classification slots. It
excludes its own value and the derived character counts. No run time, host,
duration, process ID, or telemetry is part of the package or identity. The same
database state, normalized query, and parameters therefore produce the same
identity.

Each `evidence_id` uses the same canonical serializer over the complete
evidence object with only `evidence_id` removed, then prefixes the SHA-256 hex
digest with `ev_`. `canonical_json_chars` is a fixed-point count: set that field
to the candidate count, serialize the complete canonical package, and repeat
until the serialized character length equals the field. Validators recompute
both derived character counts even though they are excluded from package
identity.

Each `evidence_id` therefore binds that evidence item's classification marker,
result rank, source conversation, source message anchor, time, hit data,
explanation, privacy-redacted excerpt, and bounded safe details. Validation
recomputes every evidence and package identity. Core validation additionally
proves the message belongs to the declared conversation and that the
privacy-normalized title, role, ISO time, and timestamp match the read-only
source row. A forged anchor or cross-wired evidence item fails closed.

`validate_memory_package_standalone(...)` proves protocol shape, canonical
derivations, and internal consistency only. Because identities are unkeyed
hashes, it is not an authenticity signature and cannot prove that a
self-consistent, rehashed package came from the source database. In particular,
a caller can replace a redacted excerpt, update its internally derived privacy
fields, and rehash a new internally consistent package.

`validate_memory_package_source_backed(..., trusted_source_db=...)` first runs
standalone validation and then establishes the stronger core boundary. It
checks the anchor against the trusted read-only database, rebuilds the title,
excerpt, and deterministic explanation with the same projection functions used
by v6.4, reruns path privacy, and compares the resulting evidence and privacy
manifest. The source-backed level therefore rejects a rehashed excerpt that no
longer matches the trusted message. Neither level is a signature.

## Protocol structure

The strict top-level keys are:

- protocol and package identity;
- formal `status` and `reason_code`;
- immutable `readonly=true` and `persisted=false` flags;
- normalized request, scope, time, and retrieval parameters;
- exact v6.4 retrieval status, counts, cursor, and availability;
- keyword, semantic, and graph capability states;
- degradation, warnings, and missing capabilities;
- deterministic privacy-redaction state;
- independently source-bound evidence;
- stable future classification slots;
- canonical JSON, evidence character, and future token-budget fields.

All retrieval evidence is explicitly `unclassified_evidence`. The `facts`,
`preferences`, `decisions`, `constraints`, `open_questions`,
`historical_states`, and `conflicts` arrays are reserved but must remain empty
in this version. A package that promotes retrieved text into one of those
arrays is invalid.

Abbreviated synthetic JSON example:

```json
{
  "protocol_version": "v7-memory-package-1",
  "package_identity": "mp_<sha256>",
  "status": "partial",
  "reason_code": "partial_capability_result",
  "readonly": true,
  "persisted": false,
  "privacy": {"absolute_path_redaction_applied": false, "redaction_count": 0, "replacement": "[absolute-path]"},
  "request": {
    "task_or_query": "alpha",
    "scope": {"memory_space": "general", "result_mode": "snippets", "hit_types": ["keyword", "semantic", "graph"]},
    "time": {"start_time": null, "end_time": null},
    "retrieval_parameters": {"context_window": 0, "limit": 20, "cursor": null, "graph_depth": 1, "relation_budget": 24, "path_budget": 12, "context_budget": 6000}
  },
  "evidence": [{
    "evidence_id": "ev_<sha256>",
    "classification": "unclassified_evidence",
    "result_rank": 1,
    "source": {
      "conversation": {"conversation_id": "conversation-1", "title": "Synthetic title"},
      "message_anchor": {"message_id": "message-2", "role": "assistant", "time": "2025-01-02T00:00:00Z", "timestamp": 1735776000.0},
      "reference_uri": "ayunos://conversation/conversation-1?message=message-2"
    },
    "hit": {"type": "keyword", "score": 1.0},
    "explanation": {"basis_code": "keyword_retrieval_evidence", "text": "Exact query terms occur in this message or its conversation title."},
    "excerpt": "alpha synthetic excerpt",
    "details": {"terms": ["alpha"]}
  }],
  "classifications": {"facts": [], "preferences": [], "decisions": [], "constraints": [], "open_questions": [], "historical_states": [], "conflicts": [], "unclassified_evidence": ["ev_<sha256>"]},
  "size": {"canonical_json_chars": 0, "evidence_excerpt_chars": 23, "future_token_budget": null, "token_selection_applied": false}
}
```

The example is abbreviated, so its placeholder identity and character count
are illustrative rather than valid. CLI JSON is the complete canonical form.

Deterministic text uses only fields already present in the validated JSON. It
does not add conclusions, summaries, inferred classifications, or hidden
message context.

## Status and degradation behavior

- `ok / package_complete`: evidence exists and every requested capability is
  complete.
- `partial / partial_capability_result`: evidence exists, but one or more
  requested capabilities are explicitly unavailable.
- `empty / no_evidence`: the source was readable but no evidence matched.
- `unavailable / <v6.4 reason>`: the source database was unavailable or
  unreadable.

Semantic and knowledge states are copied exactly from v6.4. This includes
missing/corrupt/invalid/dependency/model states,
`semantic_time_evidence_unprovable`, `direct_channel_unavailable`,
`no_direct_seed`, `graph_depth_zero`, and `hit_type_disabled`. Keyword capability derives only from the exact v6.4 source
database state. Missing semantic or knowledge capability creates a partial or
degraded package when keyword evidence remains usable; it is never represented
as a complete result.

Request validation reconstructs the shared v6.4 `RetrievalQuery` and requires
the package request to equal its canonical normalized form. ISO parsing,
inclusive UTC normalization, modes, hit types, cursor token limits, and every
numeric minimum/maximum therefore use the v6.4 implementation directly. The
v6.4 module also owns the closed top-level and per-capability status/reason-code
maps consumed by the package validator.

The v6.4 producer and v7 validator also call the same
`validate_retrieval_envelope_state(...)` state machine. With an available
source, top-level retrieval is exactly `ok/query_complete`; a requested
semantic capability cannot be `not_requested`; graph is `graph_depth_zero`
only when graph was requested with depth zero; and evidence can exist only for
requested, available capabilities. With an unavailable source, its reason
controls the top-level unavailable state, both sidecars remain
`not_requested/hit_type_disabled`, and results/evidence are forbidden. This is
a cross-field contract, not a second collection of permissive v7 enums.

The producer and validator also share `derive_direct_seed_state(...)`.
Keyword and semantic are the only direct graph-seed channels. A graph query is
reachable only when at least one requested direct channel is available and has
an actual hit. Request normalization rejects graph without keyword or semantic
using `graph_requires_direct_channel`; it never becomes a normal empty package.
`direct_channel_unavailable` means requested direct channels could not execute,
while `no_direct_seed` means at least one requested direct channel executed but
returned no direct evidence, with zero results and no graph invocation. These
preconditions take precedence over knowledge-sidecar inspection: a missing or
otherwise degraded knowledge sidecar is reported only after a legal direct seed
exists. The producer and validator share this ordering and reject envelopes that
substitute a knowledge-sidecar reason for either direct precondition.

Each graph evidence item carries its seed conversation, seed message anchor,
canonical `seed_hit_types`, and the corresponding path-start conversation and
anchor. Shared `derive_graph_seed_bindings(...)` requires that the seed key
match direct evidence in the same envelope and that `seed_hit_types` equal the
complete set at that exact key in canonical `keyword,semantic` order. Subsets,
supersets, duplicates, graph types, cross-conversation anchors, missing/directless
messages, graph-only seeds, and path-start mismatches fail closed. These fields
enter evidence identity and therefore package identity. Multiple graph items may
share one proven seed; each is checked independently. On a later page containing
graph evidence, v6.4 carries copies of the actual direct evidence needed to make
that page's seed proof self-contained without changing its result rank/count.

Standalone validation proves only this in-envelope relationship. Source-backed
validation first verifies each direct evidence anchor and projection against the
trusted read-only database, then requires every graph seed key to reference one
of those verified direct anchors. Thus it also proves that the seed message
exists and belongs to the declared conversation.

`result_count` is exactly the number of distinct current-page result ranks, and
every represented rank must have at least one evidence item. Ranks are the
contiguous range beginning at the offset encoded in the request cursor and the
page cannot exceed `limit`. `total_ranked` is the size of the complete ranked
set for that query snapshot. A non-empty page must end at or before that total,
and `next_cursor` must be the exact cursor for the page end if and only if more
ranked results remain. An empty first page requires a zero total; an empty page
reached by a formerly valid cursor may have a total no greater than its offset.
These relations are recomputed and cannot be bypassed by changing both counts
and package identity.

## Absolute-path privacy

Before evidence identity is calculated, a deterministic scanner replaces
Windows drive paths, UNC paths, POSIX absolute paths, and `file:` URIs with the
literal `[absolute-path]`. It does not terminate at whitespace. A span begins
only at one of those absolute-path prefixes outside a Unicode word/URL prefix;
balanced parentheses/brackets inside the path are consumed, while an unmatched
outer closing bracket, quote, pipe, control character, or sentence delimiter
followed by whitespace/end terminates the span and remains in the output.
Trailing whitespace is outside the replacement. If an unquoted path has no
safe delimiter, the scanner deliberately over-redacts the remaining text rather
than risk leaking a suffix directory or filename.

The operation applies recursively to conversation titles, excerpts/snippets,
explanations, and safe evidence-detail strings. It covers literal spaces in all
four path families and preserves wrapping quotes, parentheses, and punctuation.
Absolute paths in query or source identity fields fail closed because redacting
those values would make query or anchor identity ambiguous.

The JSON `privacy` manifest records whether clipping occurred, the exact number
of replacements, and the replacement token. The validator recounts replacement
tokens and rejects any remaining recognizable absolute path. The renderer reads
the same validated object and prints the same marker and redaction state, so it
cannot reintroduce the original path. Standalone validation recomputes these
values from the package it received. Source-backed validation additionally
rebuilds keyword excerpts with the shared query-centered snippet projection and
semantic/graph excerpts with the shared bounded message projection, then
recomputes the manifest from trusted source content. Errors expose only fixed
protocol text.

## Validation and privacy

Validation rejects unknown or missing schema keys, wrong protocol versions,
non-canonical requests, out-of-range budgets, invalid status/reason pairs,
non-canonical ordering/counts, identity changes, invalid capability
propagation, malformed evidence, duplicate evidence, source-reference URI
mismatch, forged anchors, evidence/source cross-wiring, dangerous extra fields,
classification-reference mismatch, and any automatic classification assertion.
Output contains no embedding vectors, raw metadata, prompt, recognizable
absolute path, or unrequested context-window message bodies. Packages exist
only in process output and are not written to disk.

SQLite WAL read-only access may update the existing `-shm` file modification
time as part of shared-memory read locking. This is disclosed as metadata
touching, not hidden as byte-level file stability; the main database, WAL
content, sidecar content, and `-shm` content remain protected and are verified
separately.
