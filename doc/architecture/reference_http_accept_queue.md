# 参照ゲームの私有HTTPサーバーと起動時burst

`LoopbackStaticServer` は所有する固定版2048だけを、episodeごとの
`127.0.0.1` / ephemeral portから配信する。private profile、served bytes、
HTTP/1.0、daemon handler、close / port解放は従来どおりとする。
`ReferenceGameHTTPServer` はlisten前に有限のbacklog 32を要求する。
これはOSへの要求値であり、OSは値をclamp / roundし得る。接続数・memory・
active handler数のhard上限を意味しない。

## 原事実と変更の根拠

Windows head `37bbafb1975e79fca7307dd621b61680403a91de`、
PR seed481 / job `124993406230` のBaselineは45秒の初期観測期限に達した。
失敗画像は全空盤面、失敗後のowned-browser診断はreadyState complete、
visible / focused、HTMLActuator constructorなし、GameManager / Input /
Storage constructorあり、tile DOM数0だった。server write summaryには
html_actuator / grid / tileの3scriptがなく、それ以外のpage / CSS / 7scriptは
HTTP200のwrite完了を記録した。seed481のpage responseは4,135 bytes、
SHA256 `aba53253f0d42866146d3c8d9a4ece502cd5c60b651340c25f7d87a480b77ff8`
で、locked indexへRNGだけを挿入した期待byte列と一致する。

この情報だけでは、3scriptのrequest未到達、header前の送信失敗、browser側の
load失敗を区別できない。45秒の失敗をRAFやbacklogの原因と断定しない。
startup deadlineの延長、空盤許可、retry / reload、Chrome flag、Planner予算、
入力 / target guardは変更しない。

Python 3.10–3.14のTCPServerは既定queue 5を使う。CPythonは
[固定commit 92f85ff3](https://github.com/python/cpython/commit/92f85ff3a07335e32a0e00a55b7b6aaf3657019b)
（GH-134249 / issue80184）で既定をSOMAXCONNへ変更した。
[一次資料のrequest_queue_size](https://github.com/python/cpython/blob/92f85ff3a07335e32a0e00a55b7b6aaf3657019b/Doc/library/socketserver.rst)
はqueueが満杯のとき新しい接続が拒否され得ることを説明する。
本私有serverでは1page・1CSS・10scriptの起動burstに有限の余裕を持たせるため
32を要求し、他のHTTP serverの既定は変更しない。selectorのpoll intervalが
全requestに固定0.5秒を加えるわけではなく、受理後のidle connectionは専用
handler threadで待つ。queueはaccept threadが一時的に実行されない区間への
余裕であり、ブラウザーのload成功を保証するものではない。

## 再現と限界

`PYTHONPATH=src:. python tools/probe_reference_http_queue.py --source <locked-source>`
はsource lockを検査し、TCP queue容量と10JSの同時HTTP client取得を比較する。
入力・DOM state・外部networkを使わず、reportは既存の耐久writerで保存する。
測定値は `doc/validation/reference_http_accept_queue_probe.json` に保存する。
Linuxの150ms accept停止中に12接続を試した3回では、queue5は6完了 / 6pending、
queue32は12完了 / 0pendingだった。socket errorはどちらも0だった。
OS依存の機構実証であり、永久にscriptが失われた再現ではない。

同一のlocked10JSを4batchずつHTTP clientで同時取得した比較は全80件
HTTP200 / byte一致だった。各batchの最大所要時間とscript / source SHAは
reportへ残す。旧queueでも最終的には全内容を配信できたため、Windowsの
missing scriptの実原因や修正後の実Windows成功をこの測定から主張しない。
先の独立probeでは120HTTP client取得と16idle preconnect保持中の10取得も
成功した。一方、別のLinux Chromium起動probeはprocessの8秒期限で未完了であり、
成功したbrowser playbackは0件だった。この失敗も
`reference_http_original_startup_probe.json` に区別して保持する。

## 失敗後だけの配信工程診断

既存の明示 `post_failure_render_diagnostics` 時だけ、固定allowlistの
requests_started / headers_completed / failed_requestsと、固定phase
request / headers / bodyを保持する。既存のresponses / completed / byte数 /
hashは維持する。開始rowをheader前に作り、未到達とheader失敗を区別する。
phase診断のOOM等は元の送信例外を上書きしない。記録失敗は事前に確保した
`recording_incomplete` をtrueへlatchし、欠落rowをrequest不在と断定しない。
他の取得済みmetadataは維持する。通常のHEAD / unsupported
methodにはGET rowを作らず、HTTPの200 / 501 semanticsを維持する。
自由なURL / Console / token / 本文を追加収集せず、counterは32bit飽和、名前と
phaseも固定集合に限る。診断はAI / Source / 学習 / leaseの入力に戻さない。


この機構probeの3module SHAは、base37bbから構成したserver-only候補の測定時点を表します。
同waveのResourceTiming追加でhelper全fileのbyte列は変わるため、保存済みprobeのhashや
数値をcombined headの測定として書き換えません。server counter classとlifecycleが
同じでも、combined profileのWindows検証は別に必要です。

## 接続counter（Issue #791、明示診断時のみ）

明示診断の時だけ、server世代ごとに次のcounterを同じlockで数えます。`ReferenceGameHTTPServer` の
class・backlog 32・HTTP/1.0・daemon handlerは変えず、instanceの `process_request` を包んで受理数を数え、
handlerの `handle_one_request` / `parse_request` で要求行の有無と解析結果を数えます。

| counter | 意味 |
|---|---|
| `connections_accepted` | `process_request` に到達した接続数 |
| `connections_awaiting_request_line` | 受理済みで要求行をまだ受信していない開いた接続数（gauge） |
| `connections_without_request_line` | 要求行が空のまま閉じた接続数 |
| `request_line_errors` | 要求行の解析に失敗した数 |
| `requests_parsed` | 要求行を解析できた要求数（allowlist外のGETも含む） |
| `requests_in_flight` | 解析済みで処理が終わっていない要求数（gauge） |

OSのlisten queueに残った接続はserverから見えないため、「browserが送っていない」と「OS queueで滞留」は
区別できません（`request_not_observed_by_server`）。診断無効時はこれらのcounterも時刻fieldも作りません。
