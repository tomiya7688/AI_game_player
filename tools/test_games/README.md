<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# AI GAME PLAYER用テストゲーム集

人間またはAI GAME PLAYERが、画面と通常のキーボード・マウス入力で遊ぶ独立したゲーム集です。AI GAME PLAYER本体の判断処理や内部状態とは接続していません。

## Windowsで起動する

Python 3.10以降と、`python -m pip` で起動できるpip 22.3以降が必要です。初回はリポジトリのルートで `run_test_games.bat -Setup` を実行してください。依存ライブラリを `build/test-games-venv` にインストールし、メニューを開きます。インストール時だけネット接続が必要です。次回からは `run_test_games.bat` をダブルクリックしてください。

`run_test_games.bat -Game othello -Seed 0` のようにゲームを直接指定できます。共通操作は R（同じseedで再開始）、P（一時停止・再開）、Esc（メニューへ戻る／メニューから終了）です。ゲーム画面からフォーカスが外れると一時停止します。AI GAME PLAYERで操作する際も、ゲーム画面を前面に置き、Pで一時停止を解除してください。

| 指定名 | ゲームと操作 |
|---|---|
| tetris | 40ラインスプリント。左右移動、下で下降、上/Xで回転、Spaceで即落下。独自の壁蹴りで、公式SRS完全準拠ではありません。 |
| othello | CPUとオセロ。黒がプレイヤー。候補マスをクリック。打てない場合は自動パス。 |
| shogi | CPUと将棋。駒と移動先をクリック。持ち駒の選択と成り選択は画面右側。入玉宣言・持将棋は未実装。 |
| chess | CPUとチェス。白がプレイヤー。駒と移動先をクリック。昇格は自動クイーン。請求可能な引き分けは自動判定。 |
| gomoku | CPUと五目並べ。交点をクリック。禁手なし、5個以上の連続で勝ち。 |
| roguelike | ターン制のダンジョン。矢印/WASDで移動、敵にぶつかると攻撃。階段への到達が目標。 |
| shooter | 2Dシューティング。矢印/WASDで移動、Spaceで射撃。3波の敵を倒す。 |
| fps | 一人称レイキャストFPS。WASDで移動、左右矢印で旋回、Spaceで射撃。敵4体を倒す。 |
| sandbox | 2Dサンドボックス。A/D・左右矢印で移動、Spaceでジャンプ。左クリックで採掘、右クリックで設置、1/2/3で素材選択。終了目標はありません。 |
| breakout | ブロック崩し。左右/A/Dまたはマウスでパドル移動、Spaceで発射。60個のブロックを破壊する。 |

## クラウド実装担当への引き継ぎ

本ゲーム集は既存のAI GAME PLAYER本体を変更せず、`tools/test_games`、`tools/test_games/tests/`、`run_test_games.bat` に追加しています。ゲームの画面は1000×760、下40ピクセルは共通操作の表示です。

オセロCPUは、その手で返せる石の数と角への加点で1手を選ぶ軽量実装です。先読みをしないため、強い対戦相手にはなりません。CPU強化は引き継ぎ時点の未対応事項です。他の盤面CPUも簡単な評価のみで、棋力の評価はしていません。

元branchの受け渡し時点では、実ウィンドウの起動だけを確認し、10種類の実操作の通し確認とAI GAME PLAYERによる自動プレイは未確認でした。Issue #634でLinux/Xvfbの実OS入力受入れを追加しました。現在の検証範囲は[統合検証記録](../../doc/独立テストゲーム集.md)を参照してください。AIによる自律攻略は引き続き未検証です。ローカル依存環境、描画PNG、レビュー資料は `build/test-games-*` にあり、Gitには含めません。

## 開発者向け確認

リポジトリのルートで以下を実行してください。Linuxでは適宜 `Scripts/python.exe` を `bin/python` に置き換えて独自の仮想環境を用意できます。Windows用起動スクリプトはLinuxでは使えません。

```powershell
python -m pip --python build/test-games-venv/Scripts/python.exe install -r tools/test_games/requirements-dev.txt
build/test-games-venv/Scripts/python.exe -m ruff format tools/test_games
build/test-games-venv/Scripts/python.exe -m ruff format --check tools/test_games
build/test-games-venv/Scripts/python.exe -m ruff check tools/test_games
build/test-games-venv/Scripts/python.exe -m pytest tools/test_games/tests -q -p no:cacheprovider
```

表示のない環境でPNGを出す場合は、`SDL_VIDEODRIVER=dummy`、`SDL_AUDIODRIVER=dummy` を設定し、`python -m tools.test_games --smoke <出力先>` を実行してください。PNG描画成功は実操作成功を意味しません。

## 作者とOSSライセンス

新規ゲーム集の作者はTomiyaです。このディレクトリ内の新規Pythonコードと起動スクリプト、および専用テストはGPL-3.0-or-laterで提供します。ライセンス本文は `LICENSE.txt` です。この指定は既存のAI GAME PLAYER本体のライセンスを変更しません。

チェスの合法手判定は [python-chess / chess](https://github.com/niklasf/python-chess)（GPL-3.0-or-later）、将棋の合法手判定は [python-shogi](https://github.com/gunyarakun/python-shogi)（固定版1.1.1の配布metadataはGPL-3.0-only）、描画は [pygame-ce](https://github.com/pygame-community/pygame-ce)（LGPL-2.1）を依存ライブラリとして利用します。バージョンは `requirements.txt` に固定しています。外部の画像・音声素材は含めていません。依存ライブラリの著作権・ライセンスは各配布物を参照してください。


## 本体・依存・配布の境界

このゲーム集と専用テストはGPLの開発fixtureです。通常アプリの`src/ai_game_player`やPython依存へは追加せず、独立した仮想環境とプロセスで起動します。本体と組み合わせる場合は、通常の画面取得とキーボード・マウス入力を使ってください。ゲームの盤面配列や座標の正解をAIのObservation/Decisionへ注入しません。

Windowsの`-Setup`は独立環境だけに依存を導入します。Linuxでは次のように起動してください。

```bash
python -m venv build/test-games-venv
build/test-games-venv/bin/python -m pip install -r tools/test_games/requirements-dev.txt
build/test-games-venv/bin/python -m tools.test_games --game shogi --seed 29
```

盤面描画はWindowsのMeiryo/Yu GothicとLinuxのNoto CJKを優先します。日本語フォントが無い場合、将棋の駒・持ち駒には`P/L/N/S/G/B/R/K`と成駒の`+`表記を使います。

依存のライセンスは[専用の権利表記](THIRD_PARTY_NOTICES.md)を参照してください。元commitの宣言、配布metadata、本文の存在を記録しています。権利・再配布条件の独立確認を完了したという意味ではありません。通常アプリ配布へこのfixtureや依存を加える場合は、#124/#228の確認が必要です。

## 再現可能な入力受入れ

通常の専用テストは52件です。X11実入力10件は、明示した隔離displayがあるときだけ起動します。ほかの作業・利用者のdisplayは指定しないでください。

```bash
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy build/test-games-venv/bin/python -m pytest tools/test_games/tests -q -p no:cacheprovider
TEST_GAMES_X11_DISPLAY=:97 TEST_GAMES_X11_EVIDENCE_DIR=build/test-games-artifacts/new-run build/test-games-venv/bin/python -m pytest tools/test_games/tests/test_x11_inputs.py -v -p no:cacheprovider
```

`new-run`には未使用の出力先を指定してください。同じゲームの既存証跡を上書きしません。X11試験は自分で起動したPIDのウィンドウとfocusを照合してOS入力を送り、XGetImageで外部からPNGを取得します。`tests/gui_probe.py`の読み取り専用oracleは試験の評価だけに使い、通常起動・AI判断には接続しません。

CIは既存のPython qualityジョブ内で専用環境を作り、規則・ライフサイクル試験とSDL dummy描画を行います。CIのPNG成功はOS入力やAI攻略の成功ではありません。実機Windows入力、全種類の長時間プレイ、汎用AIの成績は#30/#96へ残します。
