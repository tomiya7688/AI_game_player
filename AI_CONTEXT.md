# AI Context

> Codex / AI が最初に読む小さい索引。詳細仕様はここへ複製しない。

## Project
- Name: AI Game Player project (final Product/Engine name is tracked by Issue #154; do not assume Kadoka is final)
- Goal: `Screen -> State -> Decide -> Act -> Observe -> Evaluate -> next decision`
- Primary runtime: Python today; performance/realtime/OS/safety-sensitive work may move behind the C++ native runtime boundary.

## Source of Truth
- Current task / requirements: GitHub Issue
- Priority / project-wide development policy: Issue #19
- AI routing / detailed working rules: `AGENTS.md`
- Implemented capabilities / limitations: `key_info.md`
- Runtime boundary: `doc/architecture/runtime_layers.md`
- 1.0.0 scope / release acceptance: `doc/release_1_0.md`
- Source / behavior truth: code + matching tests
- Detailed feature contracts: `doc/` only when routed or required

## Read First
1. This file.
2. If no Issue was explicitly assigned, run `start_task.bat`.
3. Read `.codex/next_issue.md` or the explicitly assigned Issue.
4. Read only the target source and matching tests routed by `AGENTS.md`.
5. Open detailed docs only when source/tests/Issue do not provide required evidence.

## Stop Exploring When
Stop broad exploration once all three are known:

- **Goal**: what behavior or artifact must change.
- **Required**: constraints, interfaces, safety/compatibility rules that must be preserved.
- **Acceptance**: how the change will be validated.

If these are sufficient, implement instead of reading more repository history, docs, or Issues “just in case”. Re-open exploration only when implementation or validation reveals a concrete unknown.

## Working Rules
- Search first, read second.
- Prefer target source -> matching tests -> direct dependency -> detailed doc.
- Do not scan all Issues, docs, source files, PR history, or full diffs by default.
- Keep unrelated refactors out of the current task.
- Summaries are indexes, not replacements for source/tests/Issue requirements.
- Preserve confidence / uncertainty and safety boundaries when they are relevant to the task.
- Prefer the smallest sufficient validation before broader checks.

## Ignore Normally
Unless directly in scope, do not read:

- generated diagrams / generated context
- `.codex/` files other than the current task pack
- large logs / datasets / runtime saves / backups
- unrelated Issues and historical discussions
- full PR diffs when changed filenames / diff stat / targeted patches are enough

## Validation
- Use matching targeted tests during implementation.
- Before commit, run `finish_task.bat`.
- Standard Git/PR completion: `finish_pr.bat "commit message" next-branch`.
- If a required runtime/GUI/Windows validation cannot be executed, report it as **Unverified** instead of expanding context indefinitely.

## Context Reading Tiers
These are reading tiers, not GitHub Issue priority labels.
- C0: current Issue, acceptance criteria, safety/compatibility constraints
- C1: target source and matching tests
- C2: direct dependencies and interfaces
- C3: routed detailed docs
- C4: history, auxiliary docs, broad repository context

## Existing Reducers
Do not duplicate these mechanisms:

- Task selection/context pack: `start_task.bat` + `tools/issue_context.py`
- Task/source/doc routing: `AGENTS.md`
- Repository State / Test Impact future work: Issue #27
- Completion checks: `finish_task.bat`
- Git/PR workflow: `finish_pr.bat`

## Adoption Source
This project follows the applicable principles from `tomiya7688/ai-context-reducer` without taking it as a runtime dependency. The reducer repository is guidance; this file and `AGENTS.md` define the local application of those principles.


## Issue unit rule

Parent Issues are never selected automatically by `start_task.bat`. A Parent
contains architecture/policy/child indexes/integration acceptance only.

Implementation Issues are the normal work unit: one primary deliverable, one
focused PR, and independent validation. See Issue #19 for the current structure.
