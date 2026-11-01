# AI Context

> Codex / AI が最初に読む小さい索引。詳細仕様はここへ複製しない。

## Project
- Name: Kadoka AI Game Player
- Goal: `Screen -> State -> Decide -> Act -> Observe -> Evaluate -> next decision`
- Primary runtime: Python today; performance/realtime/OS/safety-sensitive work may move behind the C++ native runtime boundary.

## Source of Truth
- Current task / requirements: GitHub Issue
- Priority / project-wide development policy: Issue #19
- AI routing / detailed working rules: `AGENTS.md`
- Implemented capabilities / limitations: `key_info.md`
- Runtime boundary: `doc/architecture/runtime_layers.md`
- Source / behavior truth: code + matching tests
- Detailed feature contracts: `doc/` only when routed or required

## Read First
1. This file.
2. Check compact remote delta before implementation (see below).
3. Run `start_task.bat`, or `start_task.bat --issue NUMBER` for an assigned Issue.
4. Read `.codex/next_issue.md`; open the original Issue if requirements are omitted or unclear.
5. Read only the target source and matching tests routed by `AGENTS.md`.
6. Open detailed docs only when source/tests/Issue do not provide required evidence.

## Remote Delta First
- Run `git fetch origin main`, then `git rev-list --left-right --count HEAD...origin/main`.
- If remote-only commits exist, first use `git log --oneline HEAD..origin/main -10` and `git diff --stat HEAD...origin/main`; inspect only task-related changed files.
- A failed fetch means remote state is Unverified, not up to date.
- Never reset, auto-resolve conflicts, or update a dirty/diverged checkout. Fast-forward only after checking local changes and commits; implementation branches may need explicit reconciliation.

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

## Context Priority
- P0: current Issue, acceptance criteria, safety/compatibility constraints
- P1: target source and matching tests
- P2: direct dependencies and interfaces
- P3: routed detailed docs
- P4: history, auxiliary docs, broad repository context

## Existing Reducers
Do not duplicate these mechanisms:

- Task selection/context pack: `start_task.bat` + `tools/issue_context.py`
- Automatic selection reads all open Issue metadata (no bodies/discussions), skips policy/parent Issues, and orders P0-P5. Explicit `--issue` bypasses selection.
- Packs preserve Markdown headings, default to at most 8000 characters (`--max-chars 2000..16000`), and mark truncation. Character limits are not token counts. Unloaded comments and truncated requirements require original-Issue lookup when relevant; failed refresh means an old pack must not be used.
- Packs include changed repository path/status evidence from `git status` without reading diff contents, then rank module/symbol and test candidates from the current `generated/repo_map.json`. Each candidate includes its evidence and a heuristic confidence score; the score is not a probability or proof of a dependency. `finish_task.bat` remains the required project check, and the original Issue plus source/tests remain authoritative.
- If Git status cannot be read, the pack refresh fails. If the Repository Map is absent or invalid, the pack reports that limitation and omits map-based candidates; regenerate it with `python tools/analyze_repo.py` before relying on those candidates.
- Task/source/doc routing: `AGENTS.md`
- Repository State generation is implemented by `tools/analyze_repo.py` and `generated/repo_map.json` (Issue #142). Task Context Pack and Test Impact remain Issues #143/#144 under parent #27.
- Completion checks: `finish_task.bat`
- Git/PR workflow: `finish_pr.bat`

## Adoption Source
This project follows the applicable principles from `tomiya7688/ai-context-reducer` without taking it as a runtime dependency. The reducer repository is guidance; this file and `AGENTS.md` define the local application of those principles.

Reviewed upstream: `7d7fa11bca671c40768825f951102750e3d580b7` (2026-10-04). Adopted Core, metadata-first Task Routing, bounded packs, and Remote Delta First. Existing completion/policy checks are reused. The Source Structure Index is Issue #142; Task Context Pack / detailed Test Impact remain Issues #143/#144. No new toolchain or summary cache is required for this change.
