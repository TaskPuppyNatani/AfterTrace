# AfterTrace Product Specification

Status: Draft baseline for v0.1  
Product name: **AfterTrace**  
Publisher branding: **TaskPuppy Kreations**  
Primary platform model: Cross-platform desktop application  
Target operating systems: Linux, Windows, macOS

## 1. Product thesis

**AfterTrace learns from how developers work with coding agents.**

It analyzes local coding-agent session history to uncover recurring instructions, persistent working preferences, and workflow patterns, then turns evidence into actionable recommendations for persistent agent configuration.

AfterTrace is **not** primarily a chat/session viewer. Sessions are source material; findings are the product; evidence connects the two.

The long-term product loop is:

**Trace → Discover → Recommend → Adjust → Observe**

The v0.1 question is intentionally narrower:

> **What do I keep telling my coding agents that they should already know?**

## 2. Initial audience

The first target user is a heavy individual coding-agent user who has accumulated dozens or hundreds of sessions and uses one or more supported coding harnesses.

Initial harnesses:

- OpenAI Codex
- Claude Code
- OpenCode

Teams, centralized administration, enterprise observability, and shared organizational policies are explicitly out of scope for v0.1.

## 3. Product principles

### 3.1 Local first

AfterTrace processes data locally by default.

Remote inference is an explicit capability the user must enable. The application must never silently fall back from a local provider to a remote provider.

### 3.2 Evidence before conclusions

Every user-visible finding must be traceable to supporting evidence.

AfterTrace should prefer:

- original user text;
- original agent text where relevant;
- observable tool/session events;
- session/project/harness counts;
- clear explanations of why evidence was grouped.

It must avoid opaque claims and arbitrary scores.

### 3.3 Read-only source ingestion

Original harness history is read-only.

AfterTrace may maintain its own normalized local database, findings, rule drafts, and history, but source session history must not be modified.

### 3.4 Human-reviewed instruction changes

Recommendations do not directly modify `AGENTS.md`, `CLAUDE.md`, or other instruction sources.

The workflow is:

**Finding → Evidence → Save Draft → Edit → Choose Scope/Targets → Preview Diff → Apply → Observe**

Any instruction-file modification requires an explicit diff and user confirmation.

### 3.5 Harness-independent core

Harness-specific formats end at adapter boundaries.

The analysis engine operates on normalized AfterTrace domain objects rather than native Codex, Claude Code, or OpenCode formats.

### 3.6 Backend-agnostic optional AI

AfterTrace does not depend on one local AI frontend.

Supported provider architecture should accommodate, where protocol-compatible:

- Unsloth-served models;
- LM Studio;
- llama.cpp;
- Ollama;
- vLLM;
- other OpenAI-compatible local or network endpoints;
- explicitly configured remote providers.

The core application must remain useful without any LLM.

## 4. Desktop experience

The GUI is the normal product experience.

A CLI may exist for power users, automation, testing, and diagnostics, but AfterTrace is not a terminal-first product.

Primary navigation:

- Overview
- Findings
- Instructions
- Projects
- Sessions
- Sources
- Settings

## 5. Visual direction

AfterTrace uses a **dark UI as the primary visual design**.

The design should feel:

- dark;
- technical;
- polished;
- evidence-focused;
- slightly cyber;
- restrained rather than visually noisy.

Avoid generic AI-gradient styling and overuse of glow, charts, gauges, or decorative telemetry.

Suggested visual language:

- near-black main background;
- dark graphite panels;
- clean borders;
- high-legibility text;
- muted secondary text;
- restrained purple, green, and cyan accents derived from TaskPuppy Kreations branding;
- warning/error colors that remain semantically distinct.

### 5.1 Branding roles

- **TaskPuppy Kreations logo**: publisher/creator branding.
- **AfterTrace wordmark**: product branding.

The AfterTrace wordmark may use a distinctive capital `T`, including the concept of extending the `T` crossbar across the remainder of `Trace`.

Expected asset locations:

- `assets/branding/TaskPuppyLogo.png`
- `assets/mockups/aftertrace-ui-concept.png`

The UI mockup is a **visual north star**, not a pixel-perfect implementation contract.

## 6. First-run experience

The first-run flow should teach the product by producing value before dropping the user into the full dashboard.

### 6.1 Welcome

Explain:

- what AfterTrace does;
- that analysis is local by default;
- that discovered data is not automatically analyzed without user scoping.

### 6.2 Source discovery

Detect:

- Codex
- Claude Code
- OpenCode

Show detected locations and approximate session counts.

Allow manual source configuration when automatic discovery fails.

### 6.3 Project discovery and permissions

Group detected sessions into projects.

Each project supports:

- **Included**
- **Excluded**
- **Never Analyze**

`Never Analyze` means AfterTrace does not import, normalize, index, embed, summarize, cache session excerpts, send content to a provider, or include the project in aggregate analysis.

Discovery and permission are separate operations.

### 6.4 Initial scan

Show restrained progress such as:

- Reading sessions
- Normalizing histories
- Finding repeated instructions
- Comparing persistent instructions

### 6.5 First finding reveal

Do not simply show "scan complete".

Prefer a concrete first result, such as:

> You repeated variations of "run focused tests after implementation" across 11 sessions.

Then provide **View Evidence**.

### 6.6 Enter normal application

After the first finding reveal, enter the normal desktop shell.

## 7. Sources and projects

### 7.1 Sources

Sources represent installed/detected coding harness histories.

A source should expose:

- harness name;
- active/disabled state;
- detected location;
- imported session count;
- last scan;
- scan health;
- manual configuration;
- rescan.

One broken source must not disable the others.

### 7.2 Project model

A project represents the actual codebase/workspace.

A project source represents that project as observed by a specific harness.

The same repository used through Codex, Claude Code, and OpenCode is one AfterTrace project with multiple project sources.

### 7.3 Project identity

Project identity must not rely solely on raw filesystem paths.

Potential identity signals include:

Strong:
- Git repository identity;
- remote URL;
- repository metadata.

Supporting:
- normalized path;
- directory name;
- harness session metadata.

If identity is ambiguous, AfterTrace asks rather than silently merging.

### 7.4 Unclassified sessions

Sessions without a trustworthy project association remain visible as **Unclassified Sessions** and can be assigned, excluded, or used to create a project.

## 8. Scanning and local data

### 8.1 Initial scan

Selected history is imported and normalized into AfterTrace's local database.

### 8.2 Incremental scans

Later scans should detect only new, changed, or missing source sessions and re-run only affected work where possible.

The app remains usable while scanning.

### 8.3 Missing source history

Default behavior may preserve the normalized AfterTrace copy when original source history disappears.

A stricter privacy mode may later allow purge-on-source-removal behavior.

### 8.4 Local database

SQLite is the baseline local persistence technology.

AfterTrace needs to retain, at minimum:

- sources;
- projects;
- project/source relationships;
- normalized sessions;
- normalized messages;
- tool calls where available;
- findings;
- finding evidence;
- instruction sources;
- drafts;
- applied rule history;
- exclusions;
- scan checkpoints;
- analyzer versions.

## 9. v0.1 analysis

v0.1 answers:

> **What do I keep telling my coding agents that they should already know?**

### 9.1 Repeated instructions

Detect:

- exact repeated instructions;
- normalized variants;
- lexical near-duplicates;
- semantic variants when optional semantic analysis is available.

### 9.2 Distribution

Findings should consider:

- raw occurrence count;
- distinct sessions;
- distinct dates;
- distinct projects;
- harness distribution;
- temporal spread.

Distinct sessions matter more than many repetitions in one session.

### 9.3 Candidate scope

Distribution may suggest:

- project-level rule candidate;
- global rule candidate;
- harness-specific guidance candidate.

This is a recommendation, not an automatic decision.

### 9.4 Persistent instruction comparison

Compare recurring patterns with active persistent instruction sources.

A recurring pattern that is not represented persistently can become a **Persistent Rule Candidate**.

### 9.5 Noise suppression

Prefer missing a weak finding over flooding the user.

Suppress or strongly down-rank:

- generic acknowledgements;
- ordinary conversational filler;
- copied issue text;
- one-off task requirements;
- task-specific filenames by themselves;
- large pasted specifications;
- harness/system boilerplate;
- non-user-authored system/developer instructions.

A repeated instruction should normally require support across multiple distinct sessions before it becomes visible.

## 10. Finding model

Every finding should answer:

1. What did AfterTrace notice?
2. How often?
3. Where did it happen?
4. Why might it matter?
5. What can the user do about it?

Core fields include:

- identity;
- type;
- canonical pattern;
- confidence;
- first seen;
- last seen;
- occurrence count;
- distinct session count;
- affected projects;
- affected harnesses;
- evidence;
- recommendation;
- state;
- analysis provenance/version.

### 10.1 Confidence

Expose qualitative confidence only:

- High
- Medium
- Low

Do not present fake precision such as `93.726%`.

The user should be able to inspect why confidence is high/medium/low.

### 10.2 Finding lifecycle

Initial lifecycle:

**New → Reviewed → Saved → Applied → Dismissed**

A finding persists across rescans and accumulates evidence rather than cloning itself on every scan.

### 10.3 User feedback

Dismissal may optionally capture reasons such as:

- Not actually the same behavior
- Too task-specific
- Already handled
- Not useful
- Other

Feedback remains local and may improve ranking/preferences later.

## 11. Evidence viewer

Sessions are source material; findings are the product.

The evidence viewer should provide three depths:

1. **Snippet**: relevant exchange only.
2. **Context**: nearby messages/events.
3. **Full session**: available secondarily when needed.

Evidence should preserve original text wherever possible.

The UI may highlight the exact span supporting a finding.

Provide a user-facing explanation such as:

> This message was grouped with 13 similar instructions because it expresses the same scope-control intent.

Avoid exposing low-level embedding math in the normal UI.

## 12. Instructions area

The Instructions screen contains:

- **Active Instructions**
- **Drafts**
- **History**

AfterTrace thinks in **rules**, not files.

A rule can carry:

- text;
- scope;
- category;
- evidence;
- target harnesses;
- source finding;
- applied timestamp/history.

Harness-specific mapping decides where an approved rule belongs.

v0.1 should understand relevant existing instruction sources for:

- Codex;
- Claude Code;
- OpenCode.

Do not invent a proprietary `aftertrace.rules` standard.

## 13. Draft and apply workflow

Saving a recommendation creates a draft.

The user may:

- edit wording;
- choose project/global scope;
- choose target harnesses;
- inspect target files;
- preview an exact diff;
- apply explicitly;
- cancel.

No silent instruction-file rewriting.

## 14. Overview

The dashboard answers:

> **What should I pay attention to right now?**

Show:

- sessions analyzed;
- projects;
- harnesses;
- last successful scan;
- new activity since last scan;
- highest-value unresolved findings;
- recent projects;
- source health only when relevant;
- visible privacy state.

Avoid arbitrary productivity scores, model leaderboards, and decorative metric overload.

A valid empty state is:

> No strong patterns yet.

Do not lower quality thresholds merely to populate the dashboard.

## 15. Privacy and inference

### 15.1 Default

**Local Only** is the default.

No session content leaves the machine unless remote processing is explicitly enabled.

### 15.2 Provider locality

Providers are classified as:

- **LOCAL**: same machine/loopback;
- **NETWORK**: another user-controlled machine/network endpoint;
- **REMOTE**: Internet-hosted provider.

The user can verify/override classification when necessary.

### 15.3 Project policy

Project policy overrides global defaults.

Examples:

- open-source project: remote allowed;
- employer project: local only;
- sensitive project: never analyze.

### 15.4 Capability-level permissions

Remote AI permissions should ultimately be controllable by operation, such as:

- semantic clustering;
- correction classification;
- contradiction analysis;
- rule drafting;
- session summarization.

A simpler v0.1 UI may expose broader controls while keeping capability-aware architecture underneath.

### 15.5 Data minimization

Remote requests must contain the smallest task-specific payload reasonably needed.

Do not send whole sessions when a small set of relevant snippets is sufficient.

### 15.6 Outbound protection

Before remote requests:

- minimize payload;
- scan for likely secrets;
- warn/cancel on suspected credentials;
- allow preview of outbound content where practical.

Secret detection reduces risk but cannot guarantee all sensitive data is identified.

### 15.7 Telemetry

Telemetry and crash reporting are off by default.

Session content must never be part of telemetry.

### 15.8 Logging

Normal application logs must not contain full prompts/session content.

## 16. Optional model assistance

Deterministic analysis is primary.

The intended pipeline is:

**Normalized Sessions → Event/Instruction Extraction → Candidate Detection → Evidence Bundling → Optional Semantic Analysis → Finding Synthesis → Quality Gate → Published Finding**

A model is a specialist adjudicator, not the entire analysis engine.

Useful optional semantic tasks include:

- grouping paraphrased instructions;
- adjudicating borderline clusters;
- generating a concise suggested persistent rule.

Every model-assisted result records provenance.

## 17. Cross-platform requirement

AfterTrace targets:

- Linux
- Windows
- macOS

This is an architectural requirement from day one, not a later porting goal.

Use a consistent AfterTrace visual language across platforms while using native behavior where it matters:

- file/folder dialogs;
- keyboard shortcuts;
- reveal/open folder behavior;
- application data paths;
- window behavior;
- system integration.

Packaging is a later milestone, but architecture must not assume Linux-only paths or behavior.

## 18. Explicitly out of scope for v0.1

Do not add:

- recurring correction analysis;
- contradiction detection;
- instruction drift analysis;
- rule-effectiveness tracking;
- harness-specific friction analysis;
- productivity scores;
- prompt quality scores;
- provider/model leaderboard;
- team accounts;
- cloud sync;
- enterprise observability;
- IDE replacement/integration platform;
- coding agent/orchestrator;
- generic chat backup product;
- user-created analyzer plugin marketplace;
- proprietary AfterTrace instruction format;
- mobile application;
- automatic instruction edits without review.

## 19. Roadmap after v0.1

### v0.2

> **What do I keep correcting my agents for?**

Primary new capability: recurring corrections.

### v0.3

> **Are my persistent instructions contradictory or out of date?**

Primary new capabilities: contradiction and drift analysis.

### v0.4+

> **Did anything change after I adjusted my instructions?**

Primary new capability: cautious longitudinal observation, without causal overclaiming.

## 20. v0.1 definition of done

A new user can:

1. Install and launch the GUI.
2. See the privacy posture.
3. Detect Codex, Claude Code, and OpenCode histories.
4. Scope projects before ingestion.
5. Run the initial scan.
6. Discover repeated instruction patterns across real sessions.
7. Inspect original supporting evidence.
8. See whether a pattern is already represented persistently.
9. Review a suggested rule.
10. Save/edit it as a draft.
11. Select targets.
12. Preview an exact instruction-file diff.
13. Apply explicitly.
14. Rescan incrementally without reprocessing everything.

If that path works cleanly against real histories, AfterTrace v0.1 is a usable product rather than only an infrastructure milestone.
