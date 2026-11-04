Spec-Status: READY
Depends-On: none
Decision-Gaps: none
Parent: #729

# Issue #808 comparison preflightテストの実時計期限

## 成果物
`tests/test_comparison_preflight.py` のsetUpの期限を `time.monotonic() + 5` から `+ 30` へ変更する（テストのみ）。

## 入出力
製品コード `tools/run_2048_campaign.py` の `SCREEN_READY_TIMEOUT_SECONDS`（45秒）と期限検査は変更しない。テストが渡す期限は `now < deadline <= now + 45` を満たす。

## 手順
setUpの実時計期限だけを余裕のある値にする。期限切れの検証（`ready deadline expired`）は既存どおり `time.monotonic` をpatchして決定論的に行う。期限の差し替え拒否テスト（`self.deadline ± 1`）は30±1秒でも上限45秒内に収まる。

## テスト
`test_comparison_preflight.py` 全件（単独）と `finish_task.bat`。

## 完了条件
高負荷のfull suiteで当該テストが期限切れにならず、期限の意味論を検証するテストが維持される。

## 現状調査
#729 と同じ「テストの実時計予算が負荷時の所要時間を含む」種類の不安定。単独実行ではmain・PRとも成功、PR #730 前のfinish_taskでのみ失敗。

## 代替案と不採用理由
- 時計を全面patch: 既存の期限切れテストが既にpatchしており、通常経路は実時計で通すことに意味があるため不採用。
- 期限を45秒ちょうど: 上限境界と一致し `deadline <= now + 45` の判定時刻差で拒否され得るため不採用。

## 範囲外・残件
他テストの実時計予算の総点検は #729 の方針に従い個別に扱う。
