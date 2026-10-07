# AI Game Player source map

このdirectoryはPython package `ai_game_player` の現在の実装です。

> 現在はpackage rootにmoduleが多いため、Issue #350で責務別subpackageへ段階移行中です。
> このREADMEはrefactor中も「最初にどこを読むか」の入口として維持します。

## 最初に読む場所

| 目的 | 入口 |
|---|---|
| アプリを起動する | [`__main__.py`](__main__.py) |
| 現在のGUIを見る | [`app.py`](app.py) |
| 1 step / continuous loopを見る | [`pipeline.py`](pipeline.py) |
| Decision本体を見る | [`engine.py`](engine.py) |
| Model/Providerを見る | [`provider.py`](provider.py) |
| Observationの組み立てを見る | [`frame_analyzer.py`](frame_analyzer.py) |
| Safetyを見る | [`safety_guard.py`](safety_guard.py) / [`fail_safe_runtime.py`](fail_safe_runtime.py) |
| Native Runtime境界を見る | [`runtime/`](runtime/) |
| Quality/architecture checker側を見る | [`applications/quality/`](applications/quality/) |
| Test専用fixtureを見る | [`testing/`](testing/) |

## 現在の責務別map

### Observation / Perception

画面を取得して、OCR/UI/画像特徴からObservationとAction候補を作る領域です。

- [`screen_capture.py`](screen_capture.py)
- [`captured_source.py`](captured_source.py)
- [`observation_source.py`](observation_source.py)
- [`frame_analyzer.py`](frame_analyzer.py)
- [`ocr_detector.py`](ocr_detector.py)
- [`ocr_provider.py`](ocr_provider.py)
- [`ocr_recognizer.py`](ocr_recognizer.py)
- [`region_detector.py`](region_detector.py)
- [`bright_region_detector.py`](bright_region_detector.py)
- [`candidate_merger.py`](candidate_merger.py)
- [`ui_recognition.py`](ui_recognition.py)
- [`ui_embedding.py`](ui_embedding.py)
- [`perceptual_hasher.py`](perceptual_hasher.py)
- [`screen_similarity.py`](screen_similarity.py)

### Decision / Evaluation

Observationと候補をDecision Contextへまとめ、Providerの判断を検証して次のActionを選ぶ領域です。

- [`decision_context.py`](decision_context.py)
- [`pipeline.py`](pipeline.py)
- [`engine.py`](engine.py)
- [`provider.py`](provider.py)
- [`evaluator.py`](evaluator.py)
- [`evaluation_primitives.py`](evaluation_primitives.py)

### Outcome

Action後の状態変化や成功/失敗/継続を判定する領域です。

- [`outcome.py`](outcome.py)
- [`outcome_models.py`](outcome_models.py)
- [`outcome_detectors.py`](outcome_detectors.py)
- [`outcome_fusion.py`](outcome_fusion.py)

### Execution / Safety

Actionを実行し、実入力・停止・Safety invariantを守る領域です。

- [`action_executor.py`](action_executor.py)
- [`execution_mode.py`](execution_mode.py)
- [`execution_history.py`](execution_history.py)
- [`run_control.py`](run_control.py)
- [`loop_guard.py`](loop_guard.py)
- [`action_safety.py`](action_safety.py)
- [`safety_guard.py`](safety_guard.py)
- [`fail_safe_runtime.py`](fail_safe_runtime.py)
- [`windows_input.py`](windows_input.py)

### Local state / persistence

設定、履歴、知識、runtime log等のlocal dataを扱います。

- [`config.py`](config.py)
- [`history.py`](history.py)
- [`knowledge.py`](knowledge.py)
- [`runtime_log.py`](runtime_log.py)
- [`metrics.py`](metrics.py)

### Windows platform

Windows固有のWindow/Input/Capture境界です。

- [`window_selector.py`](window_selector.py)
- [`screen_capture.py`](screen_capture.py)
- [`windows_input.py`](windows_input.py)
- [`cross_process_lock.py`](cross_process_lock.py)

### Shared contracts

- [`models.py`](models.py)
- [`runtime/`](runtime/)

## 目標package layout

#350では、rootを入口だけに近づけ、以下のような責務別packageへ移行します。

```text
ai_game_player/
  __init__.py
  __main__.py
  app.py
  models.py

  perception/
  decision/
  outcome/
  execution/
  safety/
  storage/
  platform/windows/

  applications/
  runtime/
  testing/
```

移動は一括renameせず、Perception / Decision / Execution / Storage+Platformの順に独立PRで行います。

## 開発者向けの参照順

具体的なIssueを実装するときは、repository全体を先に読むのではなく、

1. `AI_CONTEXT.md`
2. 対象Issue
3. `AGENTS.md` のTask Router
4. 対象source + matching tests
5. 必要なarchitecture document

の順で必要な範囲だけ読んでください。
