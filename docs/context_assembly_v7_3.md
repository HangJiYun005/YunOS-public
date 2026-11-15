# YunOS v7.3 Context Assembly

v7.3 assembles the smallest task-relevant context presented to a future LLM.
The system owns retrieval, relevance filtering, priority, budgeting, omission,
and rendering. The model is not asked to retrieve, select, approve, or mutate
memory.

## Versioned identities

- context protocol: `v7.3-context-assembly-2`;
- selection policy: `v7.3-context-selection-policy-2`, identified by
  `csp_<sha256>`;
- assembly identity profile: `v7.3-context-assembly-identity-2`, producing
  `ctx_<sha256>`.

The v7.0 `v7-memory-package-1`, v7.1
`v7.1-evidence-selection-2` / `v7.1-selection-policy-2`, and v7.2
`v7.2-temporal-memory-candidates-4` contracts remain unchanged. v7.3 consumes
their validated packages and records all source identities in `source_chain`.

## Four context layers

The model payload has four explicit sections in fixed priority order:

1. **Current task**: the current question, deterministic query topics, time
   range, retrieval status/counts, and the references actually assembled.
2. **Confirmed user profile**: only independently supplied items with
   `approval.status=approved` and `approval.authority=user`. The production
   CLI/GET service accepts no profile input, so this layer remains empty until
   an independent approval authority exists.
3. **Candidate memory**: v7.2 candidates may be auxiliary references, but every
   item is rendered as `candidate/not_approved`. Candidate relations retain
   purchase evolution, coexistence, condition, conflict, or replacement
   context only when both candidate endpoints fit.
4. **Historical evidence**: citation URI and conversation/message/time anchor
   by default. Excerpts are copied only as complete items within the explicit
   history-expansion allowance; an excerpt is never semantically truncated.

Candidate records cannot enter the confirmed-profile layer. v7.3 implements no
approval, promotion, profile edit, automatic long-term-memory write, or model
call.

## Task relevance

The policy uses deterministic NFKC/casefold lexical topic overlap. Latin terms
match complete normalized tokens only, so `Pro` does not match `project`.
Chinese text is divided into contiguous Han segments: a two- or three-character
term matches only an exact segment, while terms of four or more characters may
match conservatively inside a segment. When this does not establish relevance,
the item is excluded. Relevance is evaluated from the current question against
approved item topics/text, candidate text plus its authenticated selected
evidence, and historical evidence text/title. The matching text is used for
selection but omitted historical bodies are not copied into the assembled
model context.

A MacBook query can therefore admit MacBook budget constraints, Pro/Air
preferences, and their temporal evolution while excluding game preferences,
novel projects, and family notes. Exclusions are body-free and use
`irrelevant_to_task`.

## Budgets and omission

| Profile | Total characters | Estimated tokens | Evidence | Candidates | History expansions |
| --- | ---: | ---: | ---: | ---: | ---: |
| Small | 2,000 | 500 | 4 | 2 | 0 |
| Medium | 6,000 | 1,500 | 12 | 6 | 0 |
| Large | 16,000 | 4,000 | 30 | 16 | 2 |

Custom accepts bounded explicit values for all five limits. Characters are a
hard limit. Tokens use the reproducible non-model estimator
`unicode-codepoint-ceil-div4-v1`; it is not an exact model tokenizer.

Allocation is current task, approved profile, candidates, candidate
relations, evidence anchors, then optional complete history expansions. If an
item does not fit, the complete item is omitted and recorded with a reason such
as `character_budget`, `estimated_token_budget`, `candidate_count_limit`,
`evidence_count_limit`, `history_expansion_limit`, or
`relation_endpoint_omitted`. v7.1 source-selection omissions are also carried
forward without bodies. Low-priority history cannot displace the current task.

An empty v7.1/v7.2 result does not erase upstream health state. Capabilities,
degradation, missing capabilities, and warnings are copied into both the
current-task layer and the v7.1 source projection. Any degraded or warning
state makes the v7.3 result `partial/degraded_source_context`, even when no
evidence was selected.

## Transparency previews

JSON includes `transparency.description = 本次回答模型看到了什么`, exact IDs for
approved profile items, candidates, evidence references, and expanded
evidence, plus the complete body-free exclusion manifest and character/token
usage. Text output reports the same identities, exclusions, usage, and embeds
the exact `model_context` between stable markers.

```powershell
python tools/context_assembly_preview.py "MacBook" --budget medium --json
python tools/context_assembly_preview.py "MacBook" --budget custom `
  --total-characters 4000 --estimated-tokens 1000 `
  --max-evidence 8 --max-candidates 4 --max-history-expansions 0
```

The GET-only preview is:

`/api/context-assembly-preview?q=MacBook&budget=medium&format=json|text`

It is available only in fixed real-database readonly mode. Neither CLI nor GET
accepts a database path, profile ledger, provider, persistence target, or
sidecar path.

All preview surfaces reuse the sealed v7.0 absolute-path detector and clipping
boundary for Windows drive paths, UNC paths, POSIX paths including `/opt` and
`/mnt`, and `file:` URIs. The same boundary covers approved statements,
candidate and history fields, source warnings, JSON, model context, renderer
output, and validation errors; absolute source paths are never exposed in a
v7.3 rendered preview.

## Trust boundary

- `standalone` validates the closed schema, layer invariants, candidate status,
  policy and assembly identities, exact renderer, counts, omissions, privacy,
  and budgets. Like earlier unkeyed identities, it proves internal consistency,
  not source authenticity.
- `source-backed` validates/reconstructs v7.0 from controlled SQLite read-only
  source data, reconstructs v7.1 and v7.2, and then reconstructs the exact v7.3
  package using an independently supplied approved-profile ledger.
- Production always uses the source-backed builder and an empty approved-profile
  input. It writes no chat database, semantic database, memory/knowledge/review
  sidecar, config, log, or model output.

v7.3 does not add an external model adapter, API adaptation layer, broad UI
change, automatic memory write, or profile mutation.
