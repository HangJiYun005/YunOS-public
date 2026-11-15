# YunOS v5.2 Summary Candidate Preview

v5.2 provides a controlled, read-only preview for conversations classified by
v5.1 change detection as `summary_missing`, `coverage_unknown`, `new_messages`,
or `source_changed`.

- The default CLI processes at most five conversations and emits candidate
  metadata only. `--limit` is capped at 50.
- Candidate text is emitted only with explicit `--show-text`.
- Each candidate binds the conversation, source snapshot hash, deterministic
  `extractive_v1` generator identity, and generator configuration hash.
- Coverage-unknown and refresh candidates expose old/candidate text hashes,
  changed state, and length delta without saving a diff.
- No formal `memory_summaries`, production sidecar, embedding, candidate file,
  API/LLM call, scheduler, or web integration is performed.

The current real accounting is 177 `summary_missing` items and 1
`coverage_unknown` item. Candidate confirmation and controlled write-back are
reserved for the next stage.

```powershell
python tools/preview_summary_candidates.py --json
python tools/preview_summary_candidates.py --limit 1 --show-text --json
```
