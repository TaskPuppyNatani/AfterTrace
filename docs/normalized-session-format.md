# AfterTrace Normalized Session Format

Status: Draft baseline for v0.1

## 1. Purpose

AfterTrace supports multiple coding harnesses with incompatible native storage formats.

The normalized session format creates a stable internal representation so that analysis code does not care whether a session originated in Codex, Claude Code, OpenCode, or a future harness.

Normalization must preserve original evidence. It must not paraphrase message content.

## 2. Design principles

1. Preserve original text wherever available.
2. Preserve source identifiers for traceability.
3. Preserve source-specific metadata without making it mandatory for generic analysis.
4. Represent missing source data explicitly rather than fabricating values.
5. Use stable enums for normalized concepts.
6. Keep raw source bytes/files outside core domain objects where possible.
7. Make the representation serializable and persistence-friendly.
8. Support incremental rescans and source-version tracking.

## 3. Source

Represents one configured harness history source.

```text
Source
  id
  harness
  display_name
  location
  enabled
  discovered_automatically
  last_scan_at
  metadata
```

`harness` initial values:

- `codex`
- `claude_code`
- `opencode`

## 4. Project

Represents one logical repository/workspace.

```text
Project
  id
  display_name
  canonical_identity
  primary_location
  git_remote
  privacy_state
  created_at
  updated_at
  metadata
```

`privacy_state` initial values:

- `included`
- `excluded`
- `never_analyze`

A project may have multiple locations over time.

## 5. ProjectSource

Associates one logical project with its representation in a specific harness/source.

```text
ProjectSource
  id
  project_id
  source_id
  source_project_identifier
  observed_path
  first_seen_at
  last_seen_at
  metadata
```

## 6. SessionReference

A lightweight source-native pointer discovered before full import.

```text
SessionReference
  source_id
  source_session_id
  source_locator
  project_hint
  started_at_hint
  fingerprint_hint
  metadata
```

It exists so discovery does not require eagerly loading every complete session.

## 7. Session

```text
Session
  id
  source_id
  source_session_id
  project_id
  started_at
  ended_at
  model
  title
  working_directory
  messages[]
  tool_calls[]
  source_fingerprint
  source_available
  imported_at
  updated_at
  metadata
```

### 7.1 Session requirements

- `source_session_id` is the harness-native identifier when available.
- `project_id` may be null while unclassified.
- `started_at`/`ended_at` may be null when source data does not provide trustworthy values.
- `model` may be null.
- `working_directory` may be null.
- `source_available` indicates whether the original source can still be found.
- `metadata` stores source-specific fields that do not belong in the generic contract.

## 8. Message

```text
Message
  id
  session_id
  sequence
  role
  timestamp
  content
  content_format
  author_source
  metadata
```

`role` initial values:

- `user`
- `assistant`
- `system`
- `developer`
- `tool`
- `unknown`

`content_format` initial values may include:

- `text`
- `markdown`
- `json`
- `mixed`
- `unknown`

`author_source` distinguishes user-authored messages from harness-injected instructions where source data makes that distinction possible.

This is important because v0.1 repeated-instruction analysis should focus on user-authored working instructions rather than harness boilerplate.

## 9. ToolCall

```text
ToolCall
  id
  session_id
  message_id
  sequence
  name
  arguments
  result_summary
  started_at
  ended_at
  status
  metadata
```

`status` may include:

- `unknown`
- `started`
- `succeeded`
- `failed`
- `cancelled`

Do not fabricate success/failure when the source does not expose it.

## 10. ObservableEvent

An optional normalized event layer may be introduced for analyzers that need more than message text.

```text
ObservableEvent
  id
  session_id
  event_type
  sequence
  timestamp
  message_id
  tool_call_id
  payload
  metadata
```

Potential future event types:

- `user_instruction`
- `agent_response`
- `tool_call`
- `file_modification`
- `test_execution`
- `test_result`
- `user_correction`
- `retry`
- `instruction_file_change`
- `completion_signal`

v0.1 does not need to implement every event type.

Observable events represent source-observable facts, not speculative conclusions.

## 11. InstructionSource

Represents a physical persistent instruction source.

```text
InstructionSource
  id
  project_id
  harness
  scope
  path
  exists
  content_fingerprint
  discovered_at
  updated_at
  metadata
```

`scope` initial values:

- `global`
- `project`
- `directory`
- `unknown`

## 12. Rule

Represents a logical behavioral instruction independent of its physical file.

```text
Rule
  id
  text
  category
  scope
  project_id
  source_ids[]
  target_harnesses[]
  state
  created_at
  updated_at
  metadata
```

Potential categories include:

- `scope_control`
- `verification`
- `git`
- `safety`
- `style`
- `workflow`
- `other`

Categories should remain extensible and must not become mandatory for early v0.1 functionality.

## 13. Finding

```text
Finding
  id
  finding_type
  canonical_pattern
  title
  summary
  confidence
  state
  first_seen_at
  last_seen_at
  occurrence_count
  distinct_session_count
  distinct_project_count
  affected_harnesses[]
  recommendation
  analysis_kind
  analysis_version
  generated_at
  last_evaluated_at
  provenance
  metadata
```

`finding_type` v0.1:

- `repeated_instruction`
- `persistent_rule_candidate`

Future types may include:

- `recurring_correction`
- `instruction_contradiction`
- `instruction_drift`
- `redundant_instruction`
- `verification_pattern`
- `harness_specific_friction`
- `rule_effectiveness_observation`

`confidence`:

- `high`
- `medium`
- `low`

`state`:

- `new`
- `reviewed`
- `saved`
- `applied`
- `dismissed`
- `archived`

Do not expose fake numeric precision as user-facing confidence.

## 14. FindingEvidence

```text
FindingEvidence
  id
  finding_id
  session_id
  message_id
  evidence_type
  original_text
  start_offset
  end_offset
  observed_at
  explanation
  metadata
```

Offsets are optional and may identify the exact relevant span within a message.

Evidence should make it possible to show:

- snippet;
- surrounding context;
- full source session where retained/available;
- why the evidence belongs to the finding.

## 15. RuleDraft

```text
RuleDraft
  id
  finding_id
  text
  category
  scope
  project_id
  target_harnesses[]
  target_instruction_sources[]
  state
  created_at
  updated_at
  metadata
```

`state`:

- `draft`
- `ready`
- `applied`
- `discarded`

A draft never modifies an instruction file by existing alone.

## 16. AppliedRule

```text
AppliedRule
  id
  rule_draft_id
  rule_id
  applied_at
  targets[]
  before_fingerprints[]
  after_fingerprints[]
  metadata
```

Persist enough information to explain what changed and support later observation/history.

## 17. AnalysisProvenance

Model-assisted and deterministic findings should be auditable.

```text
AnalysisProvenance
  deterministic_steps[]
  semantic_steps[]
  provider_id
  provider_locality
  model
  request_purpose
  analyzer_versions
  metadata
```

A deterministic-only finding may contain no provider/model fields.

## 18. Provider locality

Normalized provider locality:

- `local`
- `network`
- `remote`

This is separate from provider type/protocol.

## 19. Unknown and missing data

Never invent data for normalized fields.

Use `null`, `unknown`, or an explicit availability flag where appropriate.

Examples:

- no trustworthy model identifier → `model = null`
- no trustworthy timestamp → `timestamp = null`
- source file disappeared → `source_available = false`
- tool result cannot be classified → `status = unknown`

## 20. Metadata policy

Each normalized object may contain a `metadata` mapping for source-specific information.

Rules:

- generic analyzers must not require source-specific metadata unless explicitly documented;
- metadata must remain serializable;
- secrets should not be duplicated unnecessarily;
- avoid persisting entire raw source records in metadata merely for convenience.

## 21. Compatibility

Changes to the normalized format that alter persisted meaning require:

- database migration;
- schema/version tracking;
- tests for old-to-new migration;
- consideration of reanalysis requirements.

Do not silently reinterpret persisted historical data.
