Spec-Status: READY
Depends-On: none
Decision-Gaps: none
Parent: #729

# Issue #817 reference game HTTPサーバーの並行10 scriptテストの競合

## 成果物
`tests/test_reference_game_http_server.py::test_concurrent_ten_scripts_preserve_bytes_counters_and_port_lifetime` の待ち方の修正（テストのみ）。

## 利用者の操作
利用者の操作はありません。テストのみの変更で、実行方法は既存どおり `PYTHONPATH="src;." python -m unittest discover -s tests -p test_reference_game_http_server.py` です。

## 入出力
製品の `tools/reference_game_http_server.py`（listen backlog 32）と `tools/reference_game_2048_launcher.py` は変更しない。判定する値（responses・completed・bytes_written・failed_requests等）も変えない。

## 手順
1. サーバーのカウンターはレスポンス書込み完了後に更新されるため、クライアント受信直後に読むと `responses == 0` を観測する競合がある。全resourceのcompletedが1になるまで、期限10秒・間隔0.02秒でポーリングしてから判定する。期限切れは現在のカウンターを含むメッセージで失敗させる。
2. 10本のscript要求スレッドが同時に接続する際、負荷の高いWindowsでは2秒の `barrier.wait` と `urlopen(timeout)` が超過した。上限を15秒にする。

## テスト
該当テストを同一process内で連続100回実行して失敗0（2026-11-05実測、`runs=100 failures=0`。修正前は単独3〜12回中1〜6回失敗）。`finish_task.bat` 全体。

## 完了条件
負荷下でもテストが決定的に通り、判定値は不変で、製品コードに変更がない。

## 現状調査
修正前の失敗: `AssertionError: 0 != 1`（responses）。修正1のみでは `TimeoutError` / `ConnectionAbortedError [WinError 10053]` が残り、原因がクライアント側の2秒上限であることを確認。backlog 32は10同時接続に十分。

## 代替案と不採用理由
- 製品側でカウンターを書込み前に更新: 「書込み完了」を意味するカウンターの意味を変えるため不採用。
- 固定sleep: 負荷に依存するため不採用。

## 範囲外・残件
他テストの実時計上限の総点検は #729 の方針に従い個別に扱う。
