# Local Embedding Provider

YunOS v4-E1 supports the local-only `BAAI/bge-small-zh-v1.5` model through
`local_sentence_transformer`. The provider loads lazily on the first nonempty
embedding request and normalizes output vectors.

Defaults are `cpu` and `E:/AI/Models/sentence-transformers`. Process-only
overrides are available through `YUNOS_EMBEDDING_MODEL`,
`YUNOS_MODEL_CACHE_DIR`, and `YUNOS_EMBEDDING_DEVICE`; the model and cache
remain restricted to the approved values. No environment values are persisted.

This provider does not call an API or write a database. `deterministic_test`
remains available for pipeline tests.
