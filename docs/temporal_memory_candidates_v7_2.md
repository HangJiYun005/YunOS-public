# YunOS v7.2 Temporal Interpretation and Memory Candidates

## Scope and trust boundary

The protocol is `v7.2-temporal-memory-candidates-4`. It consumes the sealed
`v7.1-evidence-selection-2` result without changing any v7.0 or v7.1 public
field, identity, validator, or entry point. Every output record has
`state=candidate`; the package is `readonly=true`, `persisted=false`, and
`candidate_only=true`. It has no approval, acceptance, memory-write, knowledge-
write, review-sidecar initialization, or model-adapter path.

The default interpreter is `deterministic-rules-4`. It performs no network or
external API call. A private future interface accepts only the selected-evidence
envelope (selected text, identity, anchors, and evidence IDs), never the complete
selection package or omitted material. The production service exposes no source
database or provider injection. Its private test seam rejects every provider
except the built-in local deterministic provider before selection or interpretation.

```powershell
python tools/temporal_memory_candidates_preview.py "search terms" --budget medium --json
python tools/temporal_memory_candidates_preview.py "search terms" --budget medium
```

```text
GET /api/temporal-memory-candidates-preview?q=search%20terms&budget=medium&format=json
GET /api/temporal-memory-candidates-preview?q=search%20terms&budget=medium&format=text
```

Neither entry point accepts a database path or provider. The GET route is
available only when the Web process is in real and readonly mode and its
resolved database is the fixed production main database. It returns
`production_readonly_required` for test, writable, or alternate paths.

## Candidate protocol

The supported candidate types are:

| Type | Deterministic explicit cues (examples) |
| --- | --- |
| `fact` | `Fact:`, `事实：`, `Confirmed fact:` |
| `preference` | `I prefer`, `我偏好`, `我更喜欢` |
| `goal` | `Goal:`, `目标：`, `My goal` |
| `decision` | `Decision:`, `我决定`, explicit change/replacement wording |
| `constraint` | `Constraint:`, `必须`, `不得`, `must`, `cannot` |
| `acceptable_alternative` | `Acceptable alternative`, `也可以`, `可以接受` |
| `open_question` | `Open question`, `待确认`, explicit uncertainty about whether something occurred |

Before classification, one closed scope/attribution scanner requires `role=user`
and a direct user expression. It scans the complete expression before candidate
segmentation and carries inherited quote, blockquote, report, example,
hypothetical, joke, and code scopes across every segment. It applies a single
fail-closed precedence to
paired quotes, Markdown blockquotes, reported speakers, model advice,
hypothetical/`if` scopes, ordinary and rhetorical questions, examples/code,
jokes, parenthetical scopes, and meta-negated assertions. Assistant/system/tool
text and every rejected scope produce no user-profile candidate. A directly
stated open question is the only question form admitted, and it is always
ambiguous, low-confidence, and `needs_review`. The rules do not promote
generic declarative text to `fact`. Speculative cues
such as `maybe`, `perhaps`, `可能`, or `也许` set `explicitness=speculative`,
`confidence=low`, and `review_status=needs_review`. Explicit matched clauses
remain only `ready_for_review`, never approved. Purchase uncertainty is not
purchase fact, and a negative purchase statement retains negative polarity.
Polarity is computed for the primary semantic object, not from an unscoped
whole-sentence negation. In an exclusive Pro goal that excludes Air, Pro remains
positive and Air is recorded only in `excluded_targets`.
An over-limit clause is never prefix-truncated: it uses the fixed safe statement
`Source expression exceeds the safe statement limit; review anchored evidence.`
and is forced to `needs_review`; clause hash, length, anchor, and source-excerpt
commitment remain available for review.

Each candidate binds:

- the v7.1 selected candidate and all v7.0 evidence identities;
- conversation ID, message ID, reference URI, and source anchor;
- canonical observation time in UTC and the interpreted local calendar date;
- explicitness, confidence, review state, a bounded reason, and an event ID.

The event ledger is ordered by `(observed_at_utc, event_id)`. It is the compact
history representation; the protocol does not generate diary-style narrative.
Old events and candidates remain present. A later event creates a relationship
record but never deletes or expires the older record.

## Temporal relationship rules

Events are grouped by deterministic topic and target keys. Compatible choice
types (`preference`, `acceptable_alternative`, `goal`, `decision`, and
`constraint`) can therefore relate across candidate-type boundaries. Precedence
is closed and deterministic:

1. `supersedes`: only a direct user clause containing both a positive exclusive
   goal/choice and an explicit exclusion, with target and excluded target bound.
   A stronger preference (`更喜欢`, `prefer`) is not exclusive and cannot
   supersede.
2. `conflict`: the same topic and target, provably equal condition, incompatible
   explicit polarity, and bounded effective-time distance.
3. `coexistence`: explicit `both`, `coexist`, `also prefer`, `并存`, `都可以`,
   or equivalent bounded cues preserve both candidates.
4. `conditional`: conditions are explicit and differ; a conditional acceptable
   alternative may instead be represented as coexistence plus a conditional
   event marker so the primary choice remains intact.
5. `evolution`: explicit time/change wording shows movement without exclusive
   replacement, such as `previously`, `now`, `以前`, `现在`, or `changed`.
6. `unknown`: the evidence establishes none of the above. Unknown never means
   expired, false, or replaced.

Relationship records bind both candidate IDs and both event IDs. Only
`supersedes` may have `exclusive=true`. An acceptable alternative is preserved
as coexistence before any conflict inference. Conflict requires equivalent
conditions and targets, so loose topic similarity cannot manufacture a conflict.

## Character and event budgets

The limits are protocol constants and identity-bound:

| Field | Hard limit |
| --- | ---: |
| candidate/event statement | 120 Unicode code points |
| interpretation reason | 100 Unicode code points |
| interpreted events per selected evidence item | 4 |

The overflow policy is
`safe_placeholder_and_needs_review`. No generated explanation may exceed
the reason budget. The package stores structured records and commitments, not
an unbounded synthesized chronology.

## Timezone identity and change protocol

All internal event times are canonical UTC with a `Z` suffix. Local calendar
interpretation uses an IANA timezone configuration whose complete content is
identity-bound by `tzc_<sha256>` and is also included in the final `tmc_`
package identity.

The first-use instance default is:

```text
schema_version=v7.2-timezone-config-2
revision=1
country_or_region=CN
timezone=Asia/Shanghai
source=instance_default
confirmation_status=needs_user_confirmation
predecessor=null
change_reason=first_use_default
```

This reserves a future user-confirmation interaction without writing a config
in v7.2. A later in-memory configuration must use `source=user_configured`.
Revision 2 or higher can be created only from a validated previous config or a
validated previous package, must equal `previous_revision + 1`, and embeds the
complete validated previous config while binding its revision/config identity
and package identity when supplied. Country or region, timezone, confirmation
state, revision, and predecessor all enter the identity. A formatted predecessor
hash alone is invalid. Trusted-selection and source-backed validation additionally
require the caller's independently trusted previous config or package and compare
it exactly with the claimed predecessor; they never trust that claim by itself.
Empty predecessor identities and reserved all-zero or all-`f` `tzc_`/`tmc_`
payloads are invalid even under standalone validation.
Changing the timezone
changes local-date interpretation and the package identity; it does not rewrite
UTC source time or mutate an older package. Persistence of timezone preferences
is outside v7.2.

## Validation and identity

`event_id`, `candidate_id`, `relation_id`, timezone `config_identity`, and the
final `tmc_` identity use canonical UTF-8 JSON with sorted keys, no insignificant
whitespace, Unicode preserved, and SHA-256. The final identity covers the v7.1
source identity/projection, provider state, timezone config, limits, event
ledger, candidates, relationships, counts, status, and reason.

Standalone validation proves strict closed nested schemas (including event,
markers, topic/target keys, polarity, conditions and relations), candidate-only
state, local-provider state, timezone protocol, UTC/local-date derivation, excerpt commitment and
anchor binding, event/candidate/relation derivation, relationship precedence,
counts, character limits, canonical identities, and the inherited v7.0 absolute-
path privacy boundary. Like v7.0/v7.1, the identities are unkeyed: a fully
coordinated internally consistent repackage is not source authentication.

The v7.2 source projection carries and revalidates v7.1 status/reason, request,
capabilities, missing capabilities, degradation, warnings, candidate ledger,
selection identity, and v7.0 source projection. A partial/degraded v7.1 source
can never become an ok/complete v7.2 package. Source-backed validation first authenticates and reconstructs v7.0/v7.1 from
the controlled SQLite `mode=ro` source, then rebuilds the complete v7.2 package
with the declared timezone config. Production JSON/text rendering occurs only
after that chain passes.

| Validator | Proves | Does not prove |
| --- | --- | --- |
| standalone | strict internal derivation, embedded timezone-chain content, limits, privacy, candidate-only and identity consistency | independent source authentication or approval |
| trusted-selection | exact v7.2 reconstruction from an already authenticated v7.1 package plus independently supplied timezone predecessor | independent v7.1 source authentication |
| source-backed | v7.0/v7.1 source reconstruction plus exact v7.2 and independently supplied timezone-predecessor reconstruction | signatures, approval, persistence, or long-term-memory writes |

## Explicit non-goals and risk

v7.2 does not implement approval/write flows, formal long-term memory,
knowledge/review sidecars, UI profile management, model adapters, arbitrary
natural-language understanding, or implicit fact inference. The local cue
lexicon is intentionally conservative. Unmatched text produces no candidate;
ambiguous or speculative matched text requires review. Independent Sol High
review should focus on coordinated-repackage trust wording, cue false positives,
relationship precedence, Unicode/path privacy, and production zero-write proof.
