# AfterTrace

**Learn from how you work with coding agents.**

AfterTrace is a local-first, cross-platform desktop application that analyzes coding-agent session history and surfaces evidence-backed patterns in the way you instruct and configure coding agents.

The first release is built around one question:

> **What do I keep telling my coding agents that they should already know?**

AfterTrace is intended to support:

- OpenAI Codex
- Claude Code
- OpenCode

## What AfterTrace is

AfterTrace ingests selected local coding-agent history, normalizes it into a harness-independent format, and identifies recurring user instructions.

It can then show:

- what instruction pattern it noticed;
- how often it appeared;
- which sessions/projects/harnesses support it;
- the original evidence;
- whether the behavior is already represented in persistent instructions;
- a suggested persistent rule when appropriate.

The user remains in control of any instruction-file change.

## What AfterTrace is not

AfterTrace is not:

- a coding agent;
- an agent orchestrator;
- an IDE;
- a generic chat-history browser;
- a model leaderboard;
- a productivity/prompt score generator;
- a mandatory cloud service.

## Product loop

**Trace → Discover → Recommend → Adjust → Observe**

v0.1 focuses on **Discover → Recommend** for repeated instructions.

## Privacy

AfterTrace is local-first.

- Source session histories are read-only.
- Discovery does not automatically grant permission to analyze a project.
- Projects can be Included, Excluded, or Never Analyze.
- Remote AI is opt-in.
- Local-to-remote provider fallback is never silent.
- Optional AI providers are backend-agnostic rather than tied to one local frontend.

## Desktop

AfterTrace targets:

- Linux
- Windows
- macOS

The GUI is built with PySide6 and uses a dark visual design.

Visual reference:

`assets/mockups/aftertrace-ui-concept.png`

Publisher branding:

`assets/branding/TaskPuppyLogo.png`

## Repository

```text
src/aftertrace/
├── adapters/      # Codex / Claude Code / OpenCode ingestion
├── analysis/      # Repetition, clustering, recommendations
├── cli/           # Secondary CLI
├── core/          # Harness-independent domain models
├── platform/      # OS-specific discovery/system behavior
├── providers/     # Optional model provider layer
├── reports/       # Export/report generation
├── services/      # Application orchestration/persistence
├── ui/            # PySide6 GUI
└── utils/
```

See:

- `docs/product-spec.md`
- `docs/architecture.md`
- `docs/normalized-session-format.md`
- `AGENTS.md`

## Development status

**Pre-alpha / foundation milestone.**

The package, normalized models, adapter/provider contracts, versioned SQLite foundation,
and desktop navigation shell are implemented. The Codex adapter supports explicit read-only
plaintext rollout discovery, loading, and normalization. Importing, analysis, model calls,
instruction editing, and history controls in the GUI are not available yet.

Codex format coverage and deliberate limits are documented in
[`tests/fixtures/codex/README.md`](tests/fixtures/codex/README.md).

## Initial v0.1 definition

A new user should eventually be able to:

1. Launch AfterTrace.
2. Detect Codex, Claude Code, and OpenCode histories.
3. Scope projects before ingestion.
4. Scan selected local history.
5. Discover repeated instruction patterns.
6. Inspect original evidence.
7. Compare a pattern with persistent instructions.
8. Save/edit a suggested rule.
9. Preview an exact instruction-file diff.
10. Apply the change explicitly.

## Development setup

A minimal editable install is intended to look like:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Launch the foundation:

```bash
aftertrace --version
aftertrace-gui
```

`aftertrace` displays local-only status. `python -m aftertrace` provides the same CLI.
To initialize an AfterTrace-owned database explicitly, run
`aftertrace --init-db <path-to-aftertrace.sqlite3>`. Do not point this at a harness database.
Application data paths use `platformdirs` for Linux, Windows, and macOS; merely opening
the shell does not create a database or inspect histories.

Run verification in the activated environment:

```bash
python -m pytest
ruff check src tests
```

For a headless GUI test run, set `QT_QPA_PLATFORM=offscreen` before running pytest.
Publisher artwork is optional; the shell also supports the existing
`assets/branding/taskpuppy-kreations.png` asset and falls back to publisher text.

## License

License decision is pending. Do not assume redistribution terms until `LICENSE` is finalized.
