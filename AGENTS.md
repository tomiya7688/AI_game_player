# AI Game Player: Agent Guide

## Standard start: minimum context

1. Read this file.
2. Identify the target GitHub Issue. GitHub Issues are the source of truth for requirements and priority.
3. Read only the Issue, the mapped source/tests below, and any document explicitly needed for that Issue.
4. Read Issue #19 only when priority, scope, or a cross-cutting design decision is unclear. Its compressed policy is below.

Do not begin by reading the entire repository, every document, all Issues, or generated diagrams. Expand context only when the current task requires it.

## Compressed project state (Issue #19)

Goal: establish a safe closed loop for one GUI game without hand-editing candidate JSON.

```text
Screen -> State -> Decide -> Act -> Observe -> Evaluate -> next decision
```

Current P0:

- State: screen recognition, OCR, UI detection/segmentation, and Decision Context (#2, #9-#12).
- Decision: candidate-based selection and initial evaluation primitives (#8, #12, #29).
- Execution: dry-run by default, explicitly enabled live input, reliable stop and target-loss stop.
- Outcome: determine whether an action worked, progressed, failed, or repeated.
- Gate: validate the whole loop with a no-manual-candidate 10-minute E2E run (#30).

Non-negotiable constraints:

- Target small local LLMs first.
- Keep recognition, evaluation, and final decision separate.
- Preserve confidence and represent uncertainty.
- Never give an LLM unrestricted coordinates or input; use validated candidates.
- Prefer closed-loop completion over advanced learning or orchestration features.
- Log failures and disagreement as well as successes.

## Task router

| Area | Read first | Read only if needed |
|---|---|---|
| Recognition / OCR / candidates | `frame_analyzer.py`, `ocr_recognizer.py`, `ocr_detector.py`, `region_detector.py`, `candidate_merger.py`, matching `tests/test_*.py` | `doc/画像解析機能説明書.md`, `doc/OCR候補検出機能説明書.md`, `doc/候補統合機能説明書.md` |
| Capture / observation | `screen_capture.py`, `captured_source.py`, `observation_source.py`, matching tests | `doc/画面キャプチャ機能説明書.md`, `doc/観測入力機能説明書.md` |
| Decision / provider / outcome | `pipeline.py`, `engine.py`, `provider.py`, `evaluator.py`, `outcome.py`, matching tests | `doc/判断パイプライン機能説明書.md`, `doc/評価指標機能説明書.md` |
| Execution / safety | `action_executor.py`, `windows_input.py`, `run_control.py`, `execution_mode.py`, matching tests | `doc/操作実行機能説明書.md` |
| GUI | `app.py` and the directly called module/tests | README and the specific feature document only |
| Persistence / logs | `config.py`, `knowledge.py`, `history.py`, `execution_history.py`, `runtime_log.py`, matching tests | `doc/設定永続化機能説明書.md`, `doc/知識ベース機能説明書.md` |
| Generated diagrams | `tools/` generator and diagram tests | `doc/class_diagram.mmd`, `doc/sequence_diagram.mmd` only when generation or a diagram issue is in scope |

## Information source responsibilities

- `README.md`: human-facing introduction, setup, and safe usage.
- `AGENTS.md`: this AI/Codex router and compressed working policy.
- `key_info.md`: implemented capabilities and current limitations.
- `doc/`: detailed feature contracts; read on demand through the router.
- GitHub Issues: requirements, feedback, priority, and discussion. Do not recreate their detailed content in local documents.
- `doc/開発予定.md`: high-level project notes only; it does not override Issue #19 priority.

## Completion

- Follow `doc/ワークフロー/ワークフロー.md`.
- Before a code commit, run the relevant tests; for a full suite use:

```powershell
$env:PYTHONPATH = "src;."
python -m unittest discover -s tests -v
python -m compileall -q src
```

- Run `git diff --check`, update `doc/versions.md`, and create a focused PR.
- Issue #25 will automate the completion workflow. Until then, keep this checklist short and execute it rather than re-reading broad documentation.