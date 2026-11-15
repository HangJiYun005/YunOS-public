# YunOS v4-E Local Embedding Evaluation

## Scope

v4-E evaluates a local embedding pipeline only. It does not enable runtime
semantic retrieval, replace keyword retrieval, or integrate `whale_chat`.
`deterministic_test` remains available for pipeline testing.

## E1: Local Provider

The approved local provider name is `local_sentence_transformer`. It loads
`BAAI/bge-small-zh-v1.5` lazily, uses `cpu` by default, and returns normalized
512-dimensional vectors. Its cache is restricted to
`E:/AI/Models/sentence-transformers`.

Process-only configuration variables are `YUNOS_EMBEDDING_MODEL`,
`YUNOS_MODEL_CACHE_DIR`, and `YUNOS_EMBEDDING_DEVICE`. They do not persist
environment changes. The provider does not call an API or write a database.

## E2: Synthetic Quality Evaluation

The fixed non-private Chinese fixture contains 10 queries and 16 candidates.
It covers YunOS memory, whale persona memory, science fiction, STM32 hardware,
image generation, local models, computer equipment, study planning, and
semantic vectors. Expected source IDs are defined in the fixture.

The recorded evaluation results are:

| Provider | Top-1 | Hit@3 | MRR |
| --- | ---: | ---: | ---: |
| `deterministic_test` | 0.20 | 0.40 | 0.3690 |
| `local_sentence_transformer` | 0.90 | 1.00 | 0.95 |

The local provider passed the v4-E gate: Top-1 is at least 0.70, Hit@3 is at
least 0.85, and MRR exceeds `deterministic_test`. This is a fixed synthetic
evaluation, not a production semantic-quality claim.

## E3: Summary-Only Test Index

The source database is read only and only `memory_summaries` is selected. No
`messages` rows are read. The local BGE records are written only to the
gitignored test database `data/test/semantic_bge_summaries.db` after an
explicit migration and `--write`.

The initial run read 2 source summaries and inserted 2 records. A repeat run
inserted 0 and reported 2 duplicates. Read-only preview with the local provider
returned 2 candidates and no `embedding_json` field. Stored records use the
actual model identifier and dimension, so mismatched provider/model or vector
dimension combinations are rejected.

## Current Boundary

No writes were made to the production local database. It stayed summary-only
and read only. No external API or LLM was called, no Web page was modified, and
v4-F Hybrid Retrieval is deliberately not implemented.
