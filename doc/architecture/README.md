# 動作と配置の見取り図：詳しく読む入口

[リポジトリのREADMEへ戻る](../../README.md)

このフォルダーには、開発者向けの実装配置資料があります。ここでは**「ゲームをどう操作するか」**と**「どの部品がどこで動くか」**を、別の図で説明します。初めて読む人は[全体案内](../guide/README.md)から進んでください。

| 知りたいこと | 読む順番 |
|---|---|
| 恒久GUIと常駐Coreの選定、現在との移行差分 | [Qt6 Widgets / Rust Coreの決定と未実装gate](qt_rust_migration.md) |
| Player・友達（GUI発言）・ホストの権限分離とGoal発見 | [自律Playerと権限分離](autonomous_player_goal_authority.md)・[根本目標](../根本目標.md) |
| 新しい画面から次の操作までの順番 | [一回の判断と実行の流れ](flow_for_readers.md) |
| UI・Python・Windows・任意サービスの役割 | [部品と実行場所](runtime_map_for_readers.md) |
| PythonとC++の分け方を開発者として考える | [既存Runtime Architecture](runtime_layers.md)・[Native境界](native_runtime_boundary.md) |
| Native C/C++の危険API検出・clang-tidy・sanitizerの検査範囲 | [Native C++ Security Profile](native_security_profile.md) |
| 機械的な呼出し一覧 | [既存のシーケンス図](../sequence_diagram.mmd)・[クラス図](../class_diagram.mmd) |
| ゲーム固有の操作の検証 | [判断パイプライン機能説明書](../判断パイプライン機能説明書.md)・[操作実行機能説明書](../操作実行機能説明書.md) |
| Windows実ゲームでCycling・Baseline・Plannerを各600秒同条件で比較する長枠Runner・集計・統計 | [Windows長枠比較](windows_long_comparison.md) |
| 状態仮説と未知質量・仮想遷移の境界 | [BeliefSnapshot](belief_snapshot.md)・[予測とBayes更新](belief_update.md)・[仮想遷移](transition_simulation.md)・[bounded探索Core](expectimax_planning.md) |
| 操作中の軽量知識更新・Mode・証拠の条件 | [Light Adaptationの本番接続と限界](light_adaptation.md)・[Mode/Phase契約](runtime_modes.md) |
| 汎用6軸の値・欠損・候補予測と実操作の区別 | [Runtime Evaluation](runtime_evaluation.md) |
| Hook・MODへ渡すstage間データの版付き契約 | [StageEnvelope v1](stage_envelope.md) |
| stage境界でHookを実行する順序・予算・失敗時方針 | [Processing Hook Runtime](hook_runtime.md) |
| Pluginが使うHook接続点・mode・payload契約の宣言とdeny-by-defaultの判定 | [Plugin Capability Manifest v1](plugin_capability_manifest.md) |
| Session内resource（Frame/GPU cache/capture等）のlease・解放順序・pressure eviction | [Runtime Resource Lifecycle](runtime_resource_lifecycle.md) |
| 長時間Sessionで資源量が増え続けないことの判定（傾き・回収・反復baseline・unsupported扱い） | [Resource Stability Gate](resource_stability_gate.md) |
| 画面・OCR・Trace等のdata classと保存・送信の1.0既定 | [Privacy Data Inventory](privacy_data_inventory.md) |
| 候補統合で、同一IDでも別対象を統合せず理由付きで保留する条件 | [候補の同一性ガード](../design/issue-495-candidate-identity.md) |

**統合確認：** 2026-11-01、feature `77a8995e92c59c2fe3610840dfbe7438297ba44e` とmainの資料7件を照合しました。初版の確認基準と現在の接続状態を区別し、実ゲームの効果は証拠がある範囲だけ記します。 初版は2026-10-31のmain `4d8ae6f`を基準にしました。モデル稼働・Windows操作・プレイ改善は資料作成や図の存在だけでは確認できません。

## これらの図に共通する約束

- **実線の矢印：** 確認した基本のつながり。**点線：** 利用条件がある経路、または将来の設計。後者は図の説明で必ず区別します。
- 箱にはクラス名より先に、日本語で「何をする部分か」を書きます。
- 「部品が存在する」「通常の実行経路へ接続」「実機で確認済み」は異なる状態です。
- モデルが選んだ操作を直接ゲームに送る経路は描きません。回答の検証と安全確認が必要です。
- 実行受領記録と操作後の明示観測から結果を評価します。未送信・確認運転を実操作結果へ混ぜない契約は[一回の流れ](flow_for_readers.md)に記します。
- 記録しただけで「学習して強くなった」「モデルが更新された」とは書きません。

詳細なモデル名・版の調査と台帳は [#609](https://github.com/tomiya7688/AI_game_player/issues/609)、処理選択の設計は [#611](https://github.com/tomiya7688/AI_game_player/issues/611)、図とリンクの継続検査は [#614](https://github.com/tomiya7688/AI_game_player/issues/614) に分けます。

この二つの図は人が理解するための説明図です。[自動生成のシーケンス図](../sequence_diagram.mmd)とは目的が違います。自動生成図は呼出し名を追う入口ですが、仕様の意味や安全性が正しいことを自動で証明するものではありません。
