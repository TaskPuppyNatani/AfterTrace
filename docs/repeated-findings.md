# Persistent repeated-instruction Findings (M4B)

`FindingPublicationService(repository).run()` explicitly runs the complete M4A
detector and publishes qualifying Findings. Nothing invokes it on startup or import.
`publish(candidates)` accepts a **complete** detector result, never a project-filtered
subset. Both entry points reread Included imported data, validate all candidates, and
synchronize the entire result under one SQLite write transaction. Invalid or stale
candidates reject the transaction with a content-free application error.

The quality gate checks canonical identity, representative text, original spans,
authorship, normalized context, timestamps, duplicate/overlapping occurrences,
distributions, counts, and at least three distinct sessions. It validates detector
output without implementing another extractor.

Finding IDs derive from type plus canonical key; receipt IDs derive from the Finding,
message, and exact span. Evidence stores only the short instruction excerpt required
by the existing model, with normalized references and project/source/harness metadata.
Republishing synchronizes current receipts and counts while retaining lifecycle state
and creation time (`generated_at`). Identical results do not change stored rows or
`last_evaluated_at`, which records the last material analysis update.

Confidence is provisional deterministic **evidence strength**, not a statistical
probability: 3–4 sessions LOW, 5–9 MEDIUM, and 10 or more HIGH. Provenance records
`repeated_instruction:v1` and deterministic steps; provider and model remain absent.

Existing schema v2 fields suffice; no migration is required. Finding metadata contains
`currently_supported`, current project/source/harness distributions, and content-free
`origin_project_ids`. An absent candidate becomes unsupported, with zero current counts
and no current receipts. Its identity, lifecycle, and historical first/last timestamps
remain. First-seen history may predate current support; new earlier evidence can extend it.

Project lineage survives receipt removal so a later Never Analyze decision can still
locate historical derived content. Purge removes prohibited receipts and replaces cached
text, timestamps, and aggregates from remaining Included evidence. With fewer than three
remaining sessions a retained Finding is explicitly unsupported; with no permitted
receipts it is deleted. Legacy Findings retain conservative whole-Finding deletion.

`repository.findings()`, `finding_evidence(id)`, and `update_finding_state(id, state)`
provide the backend boundary. Callers must inspect `currently_supported`
when showing current results. The existing import safeguard still rejects replacement
of normalized messages referenced by evidence, preserving the old copy on failure.

## Desktop Findings (M4C)

Open **Findings → Analyze Imported History** to explicitly run this pipeline in a worker
thread. Navigating to Findings or selecting **Refresh** only reloads persisted summaries
and the eligible imported-session count; it never imports, discovers, or analyzes history.
`FindingsService` exposes current repeated-instruction Findings in repository order.
Unsupported history remains stored but is hidden from this view. Lifecycle state is
displayed without editing or hidden transitions.

**View Evidence** loads only that Finding's persisted instruction receipts and related
project/source/session metadata, in a worker. The dialog preserves original wording and
shows persisted analyzer provenance. Refresh closes and clears open evidence views;
scope operations invalidate displayed data, and late worker results cannot restore an
invalidated view. No transcript browser, recommendations, or provider calls are involved.

## Lexical near-duplicate candidates (M5A)

`NearDuplicateInstructionAnalyzer().analyze(repository.imported_messages_for_analysis())`
produces transient candidates over the same privacy-scoped normalized messages as M4A.
It reuses M4A's instruction extraction, noise suppression, explicit user-authorship
check, canonical grouping, and exact original evidence spans. Exact canonical groups are
formed first; groups below M4A's publication support floor can still contribute to a
near-duplicate cluster.

The comparison view uses NFC and case-folded tokens, expands common contractions,
removes a small set of leading polite wrappers (`please`, `can/could/would you`, and
`make sure [you]`), and maps a few completion endings such as `when you're done`,
`when finished`, or `afterward` to `after`. It does not stem words or remove
meaning-bearing terms; for example, `after implementation` and `before implementation`
remain distinct. Original wording and source offsets are never rewritten.

Compatibility requires at least two shared tokens, the same leading action after
auxiliary/negation words, matching negation state, matching presence of a small set of
scope/action markers (`focused/full`, `local/remote`, `only/all/any`, `before/after`,
`allow/deny`, and `keep/replace`), and an ordered `SequenceMatcher` token ratio of at
least **0.80** with autojunk disabled. This transparent threshold and the marker checks
intentionally prefer false negatives. For example, focused tests and focused regression
tests can group, while focused and full test suites, local and remote models, and push
versus do-not-push instructions remain separate.

Clustering visits exact groups in canonical-key order and admits a member only when it is
compatible with every existing cluster member. This complete-link rule prevents an
intermediate phrase from joining incompatible endpoints through a transitive chain.
Candidates require at least **three distinct sessions across the whole cluster**;
occurrence counts remain separate, and overlapping sessions count once toward support.
Representatives are observed wording selected by session support, occurrence count,
conciseness, and stable lexical tie-breaks. Candidate UUIDs derive from the analyzer
version and sorted member keys, so membership, evidence order, and IDs do not depend on
database or input iteration order.

M5A stops at `near_duplicate_lexical:v1` candidates. It adds no persisted Finding,
database migration, UI, provider call, embedding, model inference, or recommendation.
