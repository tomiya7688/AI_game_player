<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# ゲーム集の引き継ぎ時点の検証記録

2026-11-01（日本時間）。基準コミット: `ae5918925c076440d6f52049dd4eb07d9cf3b314`。既存の本体・文書・設定の未コミット変更は対象外とし、新規ゲーム集と専用テストだけを受け渡す。

## 検証済みの範囲

- Ruff 0.14.3: 新規Pythonコードと専用テストのformat、format --check、checkが成功。
- 専用テスト34件成功（盤面13、アーケード9、ワールド10、共通メニュー2）。メニューテストはpygameイベントによるEnter／クリック選択であり、OSの実入力テストではない。
- 全10種類のPNG描画成功。実ウィンドウの起動は確認したが、画面取得時に利用者がComputer Useを停止。実操作の通し確認とAI GAME PLAYERによる自動プレイは未確認。
- オセロCPUの強化は未対応。角への加点と即時の返せる石数だけで手を選び、先読みしない。

## 追加レビューの対象と結果

外部へ渡した範囲は `__main__.py`、`launch.ps1`、`requirements.txt` の全体。ゲームルール全体、専用テスト、後から追加したREADMEと本記録は外部レビュー対象外。この記録はゲーム集全体のレビュー完了を意味しない。

資料SHA256: `39E97362903B69AADD3DDD471163D20BF949F35F20057DFD06EB1EF20FF3EA89`。資料・結果原本はローカルの `build/test-games-evidence/reviews/` に保存している。

| 担当／モデル | 2026-11-01の実行・検出結果 | 主担当の照合 |
|---|---|---|
| Codex gpt-6-luna low | 21:12 JST開始、回答取得。`luna.json` | smokeの終了処理への指摘。正常経路にはreturn前のpygame.quitがあり、正常終了の欠落という指摘は不採用。PNG保存例外時の共通finallyは未実装。 |
| Antigravity gemini-3.8-flash-low | 21:12 JST開始、回答取得。`antigravity.json` | 古いpipでは--pythonを使えない指摘を採用し、READMEにpip 22.3以降の前提を追記。python-chessとchessの競合という指摘は、前者が後者に依存するメタパッケージであり実インストールも成功したため不採用。メニューのインデント誤りという指摘は実コードと不一致で、Enter・クリックの2テストでも起動を確認したため不採用。 |
| Continue poolside/laguna-s-2.1:free | 21:12 JST開始、HTTP 404／exit=1。`continue.json` | 未完了。再試行なし。 |
| Qwen Code | 21:10 JSTのGet-ReviewClients検出でavailable=false。旧OpenRouter経路停止、代替無料経路未設定 | 利用不可として省略。別経路へ切り替えず。 |
| Grok grok-4.7 | 21:12 JST開始、94.5秒後timeout_after_partial_response。HTTP 402/429診断。`grok.json` | 最終レビュー本文なし。未完了、再試行なし。 |
| Jules無料経路 | 21:13 JST依頼受付。session `123202298464851348`。21:15 JST時点Planning、pullはNo diff found | [依頼先](https://jules.google.com/session/123202298464851348)。本文未取得。依頼受付はレビュー完了ではない。クラウド担当への受け渡し時点で進行中。 |
| 追加: DeepSeekHarness / deepseek-account/deepseek-flash | 21:12 JST開始、Configuration/connection failure (ArgumentException)。`deepseekharness.json` | 未完了、再試行なし。 |

共通起動口（秘密を含まない引数）:

```powershell
$packet = 'E:/projects/AI_GAME_Player/build/test-games-evidence/reviews/packet.txt'
$results = 'E:/projects/AI_GAME_Player/build/test-games-evidence/reviews'
& 'E:/projects/.review-tools/Invoke-LocalReview.ps1' -Reviewer Luna -InputFile $packet -OutputFile "$results/luna.json" -PublicPacketConfirmed -TimeoutSeconds 90
# Continue / Antigravity / Grok: 各担当名を指定して同じ引数で実行した。
& 'E:/projects/.review-tools/Invoke-FreeCliReview.ps1' -Reviewer Continue -InputFile $packet -OutputFile "$results/continue.json" -PublicPacketConfirmed -TimeoutSeconds 90
& 'E:/projects/.review-tools/Invoke-FreeCliReview.ps1' -Reviewer Antigravity -InputFile $packet -OutputFile "$results/antigravity.json" -PublicPacketConfirmed -TimeoutSeconds 90
& 'E:/projects/.review-tools/Invoke-FreeCliReview.ps1' -Reviewer Grok -InputFile $packet -OutputFile "$results/grok.json" -PublicPacketConfirmed -TimeoutSeconds 90
& 'E:/projects/.review-tools/Invoke-ExtraFreeReview.ps1' -Reviewer DeepSeekHarness -InputFile $packet -OutputFile "$results/deepseekharness.json" -PublicPacketConfirmed -TimeoutSeconds 90
Get-Content -LiteralPath $packet -Raw -Encoding UTF8 | jules new --repo tomiya7688/AI_game_player
```

Haikuは共通運用手順の2026-11-01の停止指示に従い依頼しない。今回PRは作成せず、受け渡し用ブランチだけをpushする。既存PRの更新は意図していない。


## #634による追加統合・自己レビュー

元branchの履歴・当時の未確認は上記のとおり保持します。追加の統合では自己レビューで元16ファイルを確認し、ゲームごとのクラス、共有描画、launcher lifecycle、専用テストへ責務を分離しました。棋譜・ルール処理は保持し、通常入力のOS受入れと失敗時cleanup、font不足の可視fallback、GPL依存境界を追加検証しています。追加の外部モデルレビューはユーザー指示により要求していません。最新の確認範囲は[統合検証記録](../../doc/独立テストゲーム集.md)とPR本文を参照してください。
