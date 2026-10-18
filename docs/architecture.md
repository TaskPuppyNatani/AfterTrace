# AfterTrace Architecture

Status: Draft baseline for v0.1

## 1. Architectural goals

AfterTrace is a cross-platform, local-first desktop application that ingests coding-agent session histories through isolated harness adapters, normalizes them into stable domain models, performs evidence-backed analysis, and presents findings through a PySide6 GUI.

Key constraints:

- Linux, Windows, and macOS from day one.
- Original harness data is read-only.
- The core does not depend on native harness formats.
- Optional AI is provider-agnostic.
- Deterministic analysis remains useful without any model.
- Remote inference is opt-in and policy-gated.
- Findings must retain evidence and analysis provenance.
- UI/business logic boundaries must remain clean.

## 2. Top-level layers

```text
Desktop GUI / CLI
        |
Application Services
        |
Core Domain + Analysis
        |
Persistence / Provider / Platform Ports
        |
Adapters and Infrastructure
```

More concretely:

```text
PySide6 UI          CLI
     \               /
      Application Services
              |
     Core Domain Models
              |
      Analysis Engine
      /      |       \
Harness   Persistence  Provider
Adapters     (SQLite)   Layer
      \        |        /
       Platform Services
```

Dependencies point inward.

Core analysis must not import PySide6, harness-specific parser implementations, or platform-specific filesystem logic.

## 3. Repository layout

```text
AfterTrace/
├── assets/
│   ├── branding/
│   │   └── TaskPuppyLogo.png
│   ├── icons/
│   └── mockups/
│       └── aftertrace-ui-concept.png
├── docs/
│   ├── architecture.md
│   ├── normalized-session-format.md
│   └── product-spec.md
├── src/
│   └── aftertrace/
│       ├── __init__.py
│       ├── __main__.py
│       ├── adapters/
│       │   ├── base.py
│       │   ├── codex.py
│       │   ├── claude_code.py
│       │   └── opencode.py
│       ├── analysis/
│       │   ├── repetitions.py
│       │   ├── clustering.py
│       │   └── recommendations.py
│       ├── cli/
│       │   └── main.py
│       ├── core/
│       │   ├── models.py
│       │   ├── findings.py
│       │   └── errors.py
│       ├── platform/
│       │   └── paths.py
│       ├── providers/
│       │   ├── base.py
│       │   └── openai_compatible.py
│       ├── reports/
│       │   └── json_report.py
│       ├── services/
│       │   ├── database.py
│       │   └── scanner.py
│       ├── ui/
│       │   ├── app.py
│       │   └── main_window.py
│       └── utils/
├── tests/
│   ├── adapters/
│   ├── analysis/
│   ├── fixtures/
│   │   ├── codex/
│   │   ├── claude_code/
│   │   └── opencode/
│   ├── providers/
│   └── ui/
├── AGENTS.md
├── README.md
├── LICENSE
├── pyproject.toml
└── .gitignore
```

This layout may evolve, but boundaries should remain.

## 4. Core domain

Core models are harness-independent and platform-independent.

Primary concepts:

- `Source`
- `Project`
- `ProjectSource`
- `Session`
- `Message`
- `ToolCall`
- `InstructionSource`
- `Finding`
- `FindingEvidence`
- `RuleDraft`
- `AppliedRule`
- `ProviderConfig`
- `ScanState`

Core models should use typed Python dataclasses or similarly lightweight typed structures unless a stronger requirement appears.

Do not couple persistence objects directly to UI widgets.

## 5. Harness adapter boundary

Every harness adapter converts native source data into normalized AfterTrace data.

Conceptual contract:

```python
class HarnessAdapter(Protocol):
    source_name: str

    def detect(self) -> list["SourceCandidate"]: ...
    def discover_sessions(self, source: "Source") -> list["SessionReference"]: ...
    def load_raw_session(self, reference: "SessionReference") -> "RawSession": ...
    def normalize_session(self, raw: "RawSession") -> "Session": ...
```

Adapters may retain source-specific metadata in normalized metadata fields, but that metadata must not become required for generic analysis.

Initial adapters:

- Codex
- Claude Code
- OpenCode

Adapter failures are isolated. One broken adapter must not make other sources unavailable.

## 6. Platform services

Platform-specific behavior is isolated behind platform helpers/services.

Responsibilities include:

- default source discovery locations;
- application data location;
- filesystem reveal/open operations;
- OS credential-store integration where available;
- path normalization;
- native file dialogs or UI-facing platform hints.

Do not scatter operating-system checks throughout adapters and analysis code.

Project identity is distinct from project location.

## 7. Normalized session boundary

Native session formats are converted into the normalized model defined in `docs/normalized-session-format.md`.

Normalization preserves:

- original text;
- source identifiers;
- timestamps where available;
- model information where available;
- tool/action metadata where available;
- source-specific metadata that may be useful later.

Normalization must not paraphrase user/assistant text.

## 8. Persistence

Baseline persistence: SQLite.

The repository/service layer owns database access.

Conceptual data families:

```text
sources
projects
project_sources
sessions
messages
tool_calls
instruction_sources
findings
finding_evidence
rule_drafts
applied_rules
rule_history
exclusions
provider_configs
scan_state
analysis_runs
```

Exact schema should be introduced through migrations rather than treated as permanent from the first commit.

### 8.1 Source immutability

Original harness data remains outside AfterTrace and read-only.

The local normalized database is AfterTrace-owned data.

### 8.2 Analysis versions

Every generated finding records:

- analysis kind;
- analyzer version;
- generated timestamp;
- last evaluated timestamp;
- optional model/provider provenance.

This enables targeted reanalysis after analyzer improvements.

## 9. Project identity

A `Project` is the logical repository/workspace.

A `ProjectSource` links a project to a particular harness/source representation.

Identity signals may include:

- Git remote identity;
- repository metadata;
- normalized path;
- session working directory;
- source-specific project identifier.

Do not silently merge ambiguous identities.

Persist user merge/separation decisions.

## 10. Scanning

### 10.1 First scan

```text
Source discovery
    ↓
User scoping
    ↓
Session discovery
    ↓
Read native sessions
    ↓
Normalize
    ↓
Persist
    ↓
Analyze
```

### 10.2 Incremental scan

Track enough source/session fingerprint state to identify:

- new sessions;
- changed sessions;
- missing/deleted sessions.

Reprocess only affected data where feasible.

Scanning runs outside the UI thread.

The application remains usable while scanning.

### 10.3 Deletion semantics

Distinguish:

- disable source;
- remove imported source data;
- forget source entirely.

Do not overload one destructive action with all three meanings.

## 11. Analysis engine

The v0.1 pipeline is:

```text
Normalized Sessions
        ↓
Instruction Extraction
        ↓
Candidate Detection
        ↓
Evidence Bundling
        ↓
Optional Semantic Grouping
        ↓
Finding Synthesis
        ↓
Quality Gate
        ↓
Published Finding
```

### 11.1 Candidate-first design

Candidates are internal.

Most candidates should never become user-visible findings.

The quality gate considers:

- number of distinct sessions;
- temporal spread;
- project spread;
- harness spread;
- lexical/semantic cohesion;
- noise filters;
- persistent-instruction coverage.

### 11.2 Deterministic first

Deterministic stages include, where practical:

- parsing;
- exact normalization;
- lexical similarity;
- counts;
- date/session/project distribution;
- instruction-file discovery;
- evidence references.

Optional AI may assist with semantic grouping/adjudication.

### 11.3 Finding identity

A finding represents a persistent concept.

New evidence updates the finding rather than generating duplicates.

## 12. Provider architecture

AI provider support is optional.

Conceptual interface:

```python
class ModelProvider(Protocol):
    provider_id: str

    def capabilities(self) -> set[str]: ...
    def classify_locality(self) -> "ProviderLocality": ...
    def test_connection(self) -> "ProviderStatus": ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def complete(self, request: "CompletionRequest") -> "CompletionResult": ...
```

Not every provider must implement every capability.

Provider locality:

- `LOCAL`
- `NETWORK`
- `REMOTE`

The user may verify/override locality classification.

### 12.1 OpenAI-compatible backend

The first generic provider implementation may support OpenAI-compatible endpoints, allowing use with compatible local/network stacks such as:

- Unsloth serving setups;
- LM Studio;
- llama.cpp;
- Ollama compatibility modes;
- vLLM;
- other compatible servers.

Do not special-case LM Studio into core architecture.

### 12.2 Provider policy

A provider request must pass:

1. project permission;
2. operation permission;
3. provider selection/locality policy;
4. payload minimization;
5. secret screening;
6. explicit confirmation where required.

No silent local-to-remote fallback.

## 13. Privacy boundary

Privacy is enforced below the UI as application policy, not merely a visual promise.

`Never Analyze` projects are rejected before import/normalization.

Remote operations require explicit enabled policy.

Outbound payload builders should receive only the minimal source fragments needed for the operation.

Application logging defaults to metadata/event identifiers rather than raw session content.

## 14. Instruction sources and rules

AfterTrace models a logical `Rule` separately from the physical file that contains it.

The instruction subsystem is responsible for:

- discovering supported instruction sources;
- extracting candidate rules;
- recording scope;
- comparing findings with active instructions;
- staging drafts;
- mapping approved drafts to harness-specific targets;
- generating exact diffs;
- applying only after confirmation;
- recording history.

v0.1 understands relevant instruction behavior for Codex, Claude Code, and OpenCode without creating a proprietary AfterTrace instruction format.

## 15. UI architecture

PySide6 is the desktop UI framework.

UI responsibilities:

- rendering state;
- collecting user intent;
- navigation;
- showing progress/errors;
- presenting evidence;
- previewing diffs.

UI must not:

- parse harness files;
- run SQL directly from widgets;
- decide provider privacy policy;
- implement analyzer algorithms.

Application services coordinate those operations.

### 15.1 UI shell

Primary screens:

- Overview
- Findings
- Instructions
- Projects
- Sessions
- Sources
- Settings

### 15.2 Background work

Long-running operations such as scanning, normalization, and analysis must not block the UI thread.

Use explicit task/job boundaries and thread-safe result handoff.

## 16. CLI

The CLI is secondary but useful for development and automation.

Initial command direction:

```text
aftertrace scan
aftertrace analyze
aftertrace analyze --source codex
aftertrace analyze --project <path-or-id>
aftertrace report
aftertrace sources
```

CLI and GUI should call the same application services.

Do not implement duplicate business logic.

## 17. Error model

Use domain/application errors rather than leaking arbitrary parser/database/provider exceptions into the UI.

Errors should retain:

- source/harness context;
- operation;
- user-safe message;
- technical detail for diagnostics;
- whether partial results remain usable.

Partial harness failure must degrade gracefully.

## 18. Testing strategy

Tests should use sanitized fixtures.

Required early coverage:

- adapter parsing/normalization;
- malformed/truncated source sessions;
- cross-platform path handling;
- project identity decisions;
- exact/normalized repetition detection;
- noise suppression;
- finding identity across rescans;
- incremental scanning;
- never-analyze enforcement;
- provider locality/policy;
- outbound payload minimization;
- diff generation/apply safety;
- database migration behavior;
- GUI smoke tests for critical first-run and finding flows.

Prefer focused regression tests first, then broaden verification when changes cross architectural boundaries.

## 19. Packaging direction

Not a v0.1 bootstrap requirement, but architecture should permit:

Linux:
- AppImage
- Flatpak later
- optional `.deb`

Windows:
- standard installer
- standalone distribution where practical

macOS:
- application bundle / DMG
- signing/notarization as release infrastructure matures

## 20. Architecture non-goals

Do not:

- create a coding-agent orchestrator;
- create a full IDE;
- create a generic chat backup platform;
- make a cloud account mandatory;
- route data remotely without explicit permission;
- introduce fake quality/productivity scores;
- couple the core to one model frontend;
- let harness-specific formats leak into analysis code;
- let UI widgets own business logic;
- build v0.2+ analyzers into v0.1 unless required to complete a v0.1 boundary.
