# Codex rollout fixtures and supported boundary

These fixtures are entirely synthetic. No private histories, identifiers, prompts, code,
credentials, or paths were copied from local sessions. UUIDs and `/example` paths are invented.

Format confirmation used the first records of two local rollout files (keys/types only),
and the public OpenAI Codex definitions retrieved on 2026-10-17:

- [Storage locations](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/lib.rs)
- [Rollout recording and canonical first session metadata](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/recorder.rs)
- [Serialized envelope and its top-level harness metadata](https://github.com/openai/codex/blob/main/codex-rs/history/src/rollout_payload.rs)
- [Messages, text parts, function/custom calls and outputs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs)
- [Per-part provenance](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models/item_metadata.rs)
- [Session metadata, turn context, user events and command outcomes](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs)

Fixtures prune fields unrelated to this adapter. `normal.jsonl` includes multiple turns,
system/developer messages, injected user-role context, mirrored user/assistant events,
and a function call with an observable exit code. Other fixtures cover missing optional
information, a metadata-only session, partial/truncated JSONL, and mixed authorship.

## Supported behavior

- Resolve `CODEX_HOME` (including `~` expansion), or the native home plus `.codex`.
  Detect existing `sessions` and `archived_sessions` as separate source candidates.
  A configured Source location points directly at a history directory.
- Enumerate `rollout-*.jsonl` recursively in deterministic path order, without following
  symlinks beneath a source. Read at most 1 MiB of the first line for discovery hints. A bad/oversized
  header leaves a loadable reference with `header_unavailable`, not a failed source.
- Explicit loading reads a single file in binary read-only mode. Normalization has no
  filesystem, database, provider, or UI side effects. Neither operation grants project
  permission. No source is automatically loaded, imported, or analyzed by the application.
- The first readable `session_meta` is canonical. Native `id` identifies the thread;
  root `session_id`, when present, stays in metadata. Missing native values stay missing.
  Session/model context remains in Codex metadata. Model uses the latest thread-owned
  `thread_settings_applied` snapshot when present, falling back to the last turn context.
  A copied ancestor's settings cannot replace the current thread's settings, and a frozen
  turn context cannot overwrite an authoritative settings snapshot. Settings record a model
  configuration; they do not establish that an inference actually ran.
  Turn completion is not treated as session end: a resumable session has no known end time.
- Each text content part becomes a Message, preserving its exact text and attribution.
  Record/content indexes retain grouping and ordering without concatenating mixed authors.
  System/developer context and explicit injected/inherited/compaction content are not user
  authored. Unmarked historical user-role text has unknown authorship unless corroborated
  by an exact matching user-message event. Matching is one-to-one; event-only messages are
  retained and mirror events do not duplicate response messages. No substring heuristics.
- Function/custom calls retain their argument/input string. Output text becomes tool messages
  and is linked to the call when `call_id` is available. A returned output or response status
  `completed` does not prove execution success. A structured command-end exit code does.
- Blank lines are ignored. Malformed records are skipped with a count and up to 20 line
  numbers, preserving valid neighbors. No readable metadata, an empty file, or an unreadable
  source produces an application error with operation/source and content-free diagnostics.
- Fingerprints use `stat-v1:size:mtime_ns`, refreshed for the actual loaded snapshot.
  They detect size or modification-time changes and are not cryptographic content hashes.
  Same-size edits that restore modification time are outside this guarantee. No historical
  content is hashed during discovery. Files changing during loading should be retried.

## Deliberate limits

Only standalone plaintext JSONL rollouts are supported. Compressed, encrypted, opaque
compaction, non-text multimodal content, and other unknown response/event kinds are not
reconstructed. Unsupported record/content counts mark partial normalization; encrypted
payloads and whole raw records are not duplicated into metadata. Fork ancestor histories
are not resolved. This adapter preserves observed records; it does not project rollback or
compaction events into a replacement conversation. Legacy authorship without source evidence
remains unknown. These limits do not affect other adapters or trigger any fallback behavior.
