# AGENTS.md — AfterTrace

This file defines standing repository instructions for coding agents working on AfterTrace.

## Product

AfterTrace is a cross-platform, local-first desktop application that analyzes coding-agent history and produces evidence-backed workflow findings and persistent-instruction recommendations.

Read before non-trivial work:

- `docs/product-spec.md`
- `docs/architecture.md`
- `docs/normalized-session-format.md`

Use `assets/mockups/aftertrace-ui-concept.png` as the visual north star for the desktop UI. It is not a pixel-perfect contract.

## Core invariants

1. **Cross-platform is architectural.**
   - Linux, Windows, and macOS are first-class targets.
   - Do not hard-code Linux-only behavior into core logic.
   - Isolate platform-specific path/system behavior.

2. **Source histories are read-only.**
   - Never modify Codex, Claude Code, or OpenCode source history.
   - AfterTrace-owned state belongs in its own storage.

3. **Discovery is not permission.**
   - Do not ingest/analyze a project merely because it was discovered.
   - Respect Included / Excluded / Never Analyze semantics.
   - `Never Analyze` must prevent import, indexing, embedding, summarization, provider transmission, and aggregate inclusion.

4. **Local-first privacy.**
   - Local-only is the default.
   - Remote inference requires explicit permission.
   - Never silently fall back from a local provider to a remote provider.
   - Minimize outbound payloads and preserve provider/model provenance.

5. **Backend-agnostic AI.**
   - Do not couple core logic to LM Studio or any one frontend.
   - Local/network/remote providers sit behind provider interfaces.
   - Deterministic functionality must remain useful without an LLM.

6. **Harness-specific formats stop at adapter boundaries.**
   - Core/analysis code consumes normalized AfterTrace models.
   - Do not leak Codex/Claude/OpenCode record formats into generic analyzers.

7. **Evidence before conclusions.**
   - Every user-visible finding must retain inspectable evidence.
   - Preserve original text.
   - Do not fabricate causal claims or arbitrary quality/productivity scores.

8. **Rules are logical objects, not files.**
   - Instruction files are targets/sources.
   - Drafts must be human-reviewable.
   - No instruction-file modification without explicit diff + confirmation.

9. **The GUI is first-class but must remain thin.**
   - PySide6 widgets render state and collect intent.
   - UI widgets do not parse harness data, run SQL directly, implement analyzers, or decide privacy policy.
   - Long work must not block the UI thread.

10. **Scope creep is a defect.**
    - v0.1 focuses on repeated instructions and persistent-rule candidates.
    - Do not add recurring-correction, contradiction, drift, effectiveness, leaderboard, team/cloud, or orchestration features unless explicitly requested.

## Development discipline

Use the narrowest investigation and verification sufficient to complete the task correctly.

Prefer, in order:

1. current diff;
2. changed files;
3. relevant tests;
4. direct callers/callees;
5. affected shared abstractions;
6. broader subsystem only when evidence requires it;
7. repository-wide search only when needed to prove a global invariant.

Do not opportunistically refactor unrelated code, rename unrelated symbols, reformat unrelated files, or hunt unrelated defects during bounded work.

Prefer the smallest coherent change that satisfies the requirement while preserving the documented architecture.

## Verification ladder

Escalate only as needed:

1. static inspection;
2. focused unit/regression tests;
3. relevant test module/subsystem;
4. related integration tests;
5. broader integration suite;
6. full suite.

A full suite is appropriate for broad/cross-cutting changes, migrations, packaging/release work, security/privacy boundaries, concurrency/background-job changes, durability/recovery work, or final sealing.

Review the final diff before reporting completion.

## Tests

Use sanitized fixtures. Do not commit real private session histories, tokens, secrets, or proprietary source material.

Add tests for confirmed behavior, regressions, important edge cases, failure handling, and privacy/security invariants.

Prefer extending existing relevant test modules over creating redundant structures.

## ast-grep

The ast-grep skill may be available in the coding environment.

Use it only when structural search provides a concrete advantage, such as tracing definitions/references, locating call sites, or checking a repository-wide structural invariant.

Do not install/reinstall/modify ast-grep unless explicitly requested.

## Subagents

Only **GPT-6 Luna Max** subagents are permitted for Codex/gstack work on this repository.

- Maximum: **4 Luna Max subagents for the entire task**.
- Four is a ceiling, not a target.
- Straightforward or tightly bounded work should normally use **zero** subagents.
- If the subagent model cannot be confirmed as GPT-6 Luna Max, do not spawn it.
- Use delegation only when it provides a concrete benefit such as independent architecture, security/privacy, concurrency, durability/recovery, migration, difficult debugging, or verification work.
- Avoid overlapping/duplicate investigations.
- The primary agent remains responsible for checking and integrating subagent findings.

## Timing report

When elapsed timing information is directly available, report:

- `Task duration: <actual elapsed time>`
- `Start time: <timestamp>`
- `End time: <timestamp>`

Do not estimate timing. If unavailable, report:

`Task duration: unavailable`

## Completion report

For non-trivial tasks, report concisely:

- result;
- files changed;
- key behavior/architecture decisions;
- verification performed and results;
- remaining known limitations or follow-up work;
- task duration.

Do not claim success without evidence.
