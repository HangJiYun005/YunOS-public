# YunOS v4.0.1 Semantic Memory Hardening

YunOS v4.0.1 seals the semantic-memory safety and reliability foundation without changing the default legacy retrieval behavior.

## Scope

- A: semantic writes and migrations are limited to the project-controlled `data/test` root. External paths, the real database, symlinks, junctions, reparse points, and hardlink aliases are rejected.
- B: every embedding records provider, model, revision, immutable local fingerprint, protocol, dimension, and normalization state. The approved local model is `BAAI/bge-small-zh-v1.5`, revision `7999e1d...`, short fingerprint `1854989a20e6`, protocol `sentence_transformers_normalized_v1`, dimension `512`. Local loading is offline-only.
- C: schema, vector, SQLite, and query failures use `SemanticRetrievalError`. Explicit hybrid mode reports a warning and rebuilds the complete legacy context as `legacy_fallback`.
- D: default legacy prompt construction has an exact golden regression. A six-case redacted evaluation runs the actual keyword, semantic, and hybrid functions on temporary databases.

## Evidence

The D evaluation measured keyword, semantic, and hybrid Hit@3 at `1.00/1.00/1.00`, all three nonempty rates at `1.00`, and `mixed_source_rate=1.00`; the quality gate passed. These are small redacted pipeline checks, not production semantic-quality proof.

The default mode remains `legacy`. Hybrid retrieval is explicit, semantic indexing remains summary-only, real databases remain read-only, and no API/LLM call or runtime setting persistence is introduced.

## Handoff

Sol's three High findings are addressed by A-C. The five v5 entry checks are satisfied. Non-blocking retrieval quality, corpus scale, and future model/version operations remain v5 work.
