# ai_game_player source map

このdirectoryはPython runtimeの本体です。

現在は歴史的に `src/ai_game_player/` 直下へmoduleが増えており、責務別subpackageへの段階移行を進めます。移行中でも読む場所が分かるよう、このREADMEをsource入口として使います。

## まず読む場所

| 目的 | 入口 |
|---|---|
| Application全体の組み立て | [`app.py`](app.py), [`applications/`](applications/) |
| UI | [`ui/`](ui/) |
| Native Runtime境界 | [`runtime/`](runtime/) |
| Capture / Observation | [`screen_capture.py`](screen_capture.py), [`captured_source.py`](captured_source.py), [`observation_source.py`](observation_source.py) |
| Recognition / OCR / UI認識 | [`frame_analyzer.py`](frame_analyzer.py), [`ocr_provider.py`](ocr_provider.py), [`ocr_recognizer.py`](ocr_recognizer.py), [`ui_recognition.py`](ui_recognition.py) |
| Decision / Provider | [`pipeline.py`](pipeline.py), [`engine.py`](engine.py), [`provider.py`](provider.py), [`decision_context.py`](decision_context.py) |
| Evaluation / Outcome | [`evaluator.py`](evaluator.py), [`evaluation_primitives.py`](evaluation_primitives.py), [`outcome.py`](outcome.py), [`outcome_fusion.py`](outcome_fusion.py) |
| Execution / Safety | [`action_executor.py`](action_executor.py), [`action_safety.py`](action_safety.py), [`safety_guard.py`](safety_guard.py), [`fail_safe_runtime.py`](fail_safe_runtime.py), [`windows_input.py`](windows_input.py) |
| Experience / Learning | [`experience.py`](experience.py), [`experience_reader.py`](experience_reader.py), [`learning.py`](learning.py), [`learning_dataset.py`](learning_dataset.py) |
| Persistence / Artifacts | [`artifact_store.py`](artifact_store.py), [`history.py`](history.py), [`knowledge.py`](knowledge.py), [`runtime_log.py`](runtime_log.py) |

## Core flow

```text
Capture
 -> Recognition / State
 -> Decision Context
 -> Provider / Decision
 -> Safety
 -> Execution
 -> Outcome
 -> next Observation
```

主な実装入口:

```text
screen_capture.py / captured_source.py
        ↓
frame_analyzer.py
        ↓
decision_context.py
        ↓
engine.py / provider.py
        ↓
pipeline.py
        ↓
action_safety.py / safety_guard.py
        ↓
action_executor.py / windows_input.py
        ↓
outcome*.py
```

## Planned package layout

source rootを薄くするため、Issue #356で次の方向へ段階移行します。

```text
ai_game_player/
  __init__.py
  __main__.py
  app.py
  applications/
  capture/
  perception/
  decision/
  evaluation/
  execution/
  safety/
  data/
  experience/
  learning/
  runtime/
  ui/
```

一度に全fileを移動せず、import/testを維持しながら領域ごとに移します。

- #358 Capture / Perception
- #359 Decision / Evaluation
- #360 Execution / Safety
- #361 Data / Experience / Learning
- #362 Public import facade / root cleanup
- #363 Source layout CI gate

## 新しいmoduleを追加する時

既存の責務subpackageがある場合、`src/ai_game_player/` rootへ新しい実装moduleを追加せず、該当packageへ置いてください。

移行完了までは既存flat moduleが残りますが、「既存がflatだから新規もflat」は採用しません。

## Tests

対応するtestはrepository rootの [`tests/`](../../tests/) にあります。

変更時は対象moduleのtestを先に実行し、完了前にprojectの通常completion checksを通してください。
