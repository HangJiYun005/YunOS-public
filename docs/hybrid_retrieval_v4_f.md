# YunOS v4-F Hybrid Retrieval Seal

## Scope

v4-F completes the read-only hybrid retrieval preview line. It combines the
existing keyword retriever with local BGE semantic retrieval over summary
records. It does not alter keyword rules, write databases, call an external
API or LLM, or automatically enable runtime settings.

## F1: Controlled Hybrid Preview

F1 uses the existing whale-scoped keyword retriever and the local BGE summary
index. Results remain separated as `keyword_results` and `semantic_results`,
then are combined with Reciprocal Rank Fusion:

`RRF = sum(1 / (60 + rank))`

The preview keeps `source_platform` provenance separate from `memory_space`
scope and does not expose full messages or embedding vectors.

## F2: Evaluation

The fixed evaluation set contains eight non-private synthetic Chinese cases,
including natural questions and short keyword queries across whale memory,
YunOS, STM32, science fiction, image generation, and local models.

| Metric | Result |
| --- | ---: |
| Keyword Hit@3 | 0.75 |
| Semantic Hit@3 | 1.00 |
| Hybrid Hit@3 | 1.00 |
| Natural keyword nonempty | 0.50 |
| Short keyword nonempty | 1.00 |
| Mixed-source rate | 0.75 |
| Keyword query gap | true |
| Quality gate | passed |

The keyword query gap is recorded only. It is not automatically repaired in
v4.

## F3: Controlled Runtime Integration

Prompt construction accepts explicit `retrieval_mode=legacy|hybrid`; legacy is
the default and preserves the existing sections and ordering. Hybrid mode uses
one `[Hybrid Relevant Memory - whale]` section and reports keyword, semantic,
and combined counts. Missing semantic indexes or provider/model mismatches
fall back visibly to `retrieval_mode_effective=legacy_fallback` with a warning.

The real read-only acceptance produced keyword/semantic/combined counts of
`0/2/2`. This validates the preview path, not production semantic quality.

## v4 Closure

v4-A through v4-F are complete. The v4 safety boundary remains: no messages
batch embedding, no database writes, no automatic runtime activation, no
whale_chat answer-chain replacement, and no external model/API dependency.

YunOS v4 is formally closed. The next planned line is v5.
