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
