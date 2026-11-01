# 固定版2048の発光・得点認識回帰（Issue #659）

この修正は任意の参照2048 Adapterの画像認識に限定する。Minecraft・カードゲーム・FPS等の汎用Planner、Belief、Verifier、Safetyに盤面固有の規則を加えない。入力は同一frameのBGRAとOCR矩形で、DOM・保存状態・予測盤面で未知セルを埋めない。

## 実画像の失敗と正解根拠

測定source `bb8648866b791c346d32e2068340ab0474b3563f`、失敗cohort `run-3de18f03ecfd4897b4b8edc87d08c2cc` は保持する。seed479は135送信後、seed480は131送信後の256発光で停止した。修正前はROIがそれぞれ `(64,274,511,511)` と `(75,274,511,511)` へ拡大し、セルがNoneになった。画面上の正しい盤面は両方とも `(75,274,500,500)`。

`tests/fixtures/2048/glow-256*` は元PNGと未加工BGRAのzlib・寸法・SHA-256・視認した16セルを保存する。seed479のrow2/col0は空ではなく2である。同じ画像の既知2と暗い字形画素が双方537個、横1px整列後の対称差0で一致することも、分類器と独立に検証する。期待値を分類器自身の出力から作らない。

## 手法と実装・接続

| 要求・手法 | 実装・接続 | 検証と範囲 |
|---|---|---|
| 局所外れ値に頑健な矩形推定 | `game_2048_board._robust_board_bounds` が行の左右端の中央値を使う。盤面幅の過半数を覆い、少なくとも片辺が既存の余白許容内で一致する行で上下端を推定する | min/maxの局所発光による包絡拡大を防ぐ。正方形・密度の既存閾値は維持 |
| 色と幾何の整合 | 既存palette、色距離、行gap、cellの4小patch照合を維持。領域外の行・行幅を密度に加算しない | seed/手数条件や未知色の近似割当を追加しない。未知セルがあるAdapterは候補を出さない |
| OCR layout association | `game_2048_adapter._read_score` がSCOREラベルと水平に重なり、直下にある数値を対応付ける。ラベル/数字の低い方のconfidenceを採用 | BESTや見出しを得点にしない。異なる複数数値はunknown。位置なしは同一文字列の明示SCORE数値だけを採用 |
| 同frame evidenceの保全 | `BrowserScreenSource` がOCR wordのbbox/confidenceを`ocr_candidates`に保持 | 比較probeも本番FrameAnalyzerと同じ証拠形式をAdapterへ渡す。OCR未実行のscoreはunknown |
| 再現画像と負例 | 元失敗2枚、固定CSSの256/512/1024/2048、0.75/1/1.25倍、四隅、overlay、未知色・曖昧scoreを回帰化 | 合成状態・DOM boundsはfixture生成時の独立oracleだけ。認識器入力は画素のみ |

中央値による端点推定は局所汚染に対する頑健統計、過半数幅と辺の照合は幾何の支持検査である。単一の最外画素を矩形へ採用しない。画像の大半が未知・遮蔽された場合の復元を保証せず、Noneを保持する。色距離・認識confidenceの閾値緩和や、以前の盤面による補完は行わない。

## 費用と再現手順

既存のPython認識境界へ数百行分の端点統計を加える。既存の全画素走査はO(W×H)、追加処理はO(H log H)時間/O(H)補助領域で、新たなフル画像コピー、FFI、依存packageはない。既存Native境界を細粒度呼出しに分割しない。

650×850元画像を10回warm replayした同一Linux環境の参考値は、旧中央値111.25ms・新110.69ms、Python peak allocation 73,352→73,472byte。既存frame bufferとブラウザー資源は含めず、Windows latencyや一般ゲームの性能保証ではない。

```bash
PYTHONPATH=src:.:tests python -m unittest test_2048_glow test_2048_gdi_row_gap test_2048_terminal_render -v
PYTHONPATH=src:. python tools/render_2048_glow_fixture.py --output build/glow-fixtures
```

画像生成はPillow/Playwrightと隔離Chromiumを使う明示的な開発操作で、通常runtimeの依存へ追加しない。生成元は既存lockのGabriele Cirulli 2048、ライセンス表記は`tests/fixtures/2048/README.md`を参照する。

**Core:** 元画像の修正前FAIL、修正後PASS、未知色と曖昧scoreの拒否を検査する。**Runtime:** 既存Adapterと比較probeへ接続し、入力許可・Stopを維持する。**Real-play:** source `5ed5cf5a11ed3b35738fb777204ca49b5912e079` の独立cohortで、seed479/480が元の135/131手を越え各180手capまで進み、全360遷移を照合した。旧失敗を成功へ読み替えない。Windows WGC/Win32や全ゲームの認識・攻略精度はこの修正の受入れではなく、#30/#96/#480等の未達として残す。


## 修正版depth1の実画面受入れ

cohort `run-fb9ac0a0c93a43a0a0b2f793adc9e698` は、同じdepth1/node10000/400ms/1000ms settle・画像だけの認識経路で測定した。world unknown massは1のまま、conditional opt-inの比較であり、確率校正済みの攻略精度とはしない。

| seed | 実送信 / unique観測完了 / 遷移一致 | UI得点 / 最大tile | invalid・未検証・pending・Stop後入力 |
|---|---|---|---|
| 479 | 180 / 180 / 180 | 2284 / 256 | 各0 |
| 480 | 180 / 180 / 180 | 2260 / 256 | 各0 |

両episodeとも180手上限で終了し、勝利・game overは観測していない。全receiptのOutcomeはunknownで、因果成功や学習改善の正解へ変換しない。Sourceの両端headは5ed5、dirty文字列は両端空、source/harness/sensor hashは一致し、`source_consistent=true`、`success=true`、cleanup errorなし。

[測定要約JSON](../validation/recognition_659_screen_5ed5.json)は全reportのSHA-256と条件・資源測定のscopeを保持する。完全reportは開発環境の`build/transition-2048/issue-659-fixed-screen/run-fb9ac0a0c93a43a0a0b2f793adc9e698/report.json`。環境切替後に元exec session IDが失われたためOS終了codeの直接取得はUnverifiedであり、mainのsuccess=true→return0分岐と最終reportログを確認した範囲にとどめる。

この測定後、親#565/#566/#567の数値wave（凸平均の1ULP区間逸脱修正）を通常mergeした。5ed5のdepth1証拠を、後続headのdepth2探索やWindows受入れの代用にしない。5ed5のWindows CIで残った探索深さと短時間比較の失敗は、新headの両event CIで再検証する。


## 同じ認識コードでの深さ2比較

source `e6c7b66a886fe35f1dba9b0d2bec6e04514df02d` では、本認識修正と同じRecognizer/Adapterを使い、既定のdepth2/node10000/400msを固定して3 seed×3 providerを比較した。[条件・全9試行の測定要約](../validation/planning_browser_depth2_e6c7.json)は両端head・source hash・clean状態を保持する。

| provider | seed479 / 480 / 481 の得点 | 平均 | 実操作合計 / 最大tile |
|---|---|---|---|
| Cycling | 1332 / 1140 / 1256 | 1242.67 | 404 / 各128 |
| Baseline | 664 / 1344 / 1448 | 1152 | 393 / 64,128,128 |
| Planner | 2268 / 2244 / 2268 | 2260 | 540 / 各256 |

Plannerの全540操作はreceipt・明示完了観測・遷移照合と一致し、no-opとStop後入力は0。519操作はdepth2、21操作は予算内に完成したdepth1を使った。全9試行/1337操作で合成点和が、判断後の独立UI得点と一致した。Plannerは全3試行が180手capで生存し、勝利ではない。各provider n=3のこの条件での記述的比較で、4CPU quotaを3seed processで共有したため資源測定は隔離されていない。未知world mass1・conditional opt-inを保ち、因果学習や全ゲーム性能へ拡張しない。

Windowsの評価専用得点は、同frameのSCOREラベルから数字行を異なる2幅で切り出し、3倍拡大/PSM7の一致・端切れ拒否を使う。[実2画像での補助OCR記録](../validation/planning_score_line_ocr.json)と、screen/window座標の倍率・offset回帰を保持する。全文OCRでラベル自体が不明な場合は、同frameのboard上段ROIを3倍に拡大しPSM6でSCORE literalだけを再読する。独立Tesseract CLIで元のSCORE候補を除去しても、元2画像と倍率2/offset付きの計4ケースが約0.96で回復することを確認した。なお読めない・複数候補・端切れ・幅間不一致はunknownを保つ。confidence0.65、予算、TTL、SourceとSafetyの基準は維持し、新headのWindows受入れは引き続き別途検証する。

#567の[追加Headless depth2 fixture測定](../validation/planning_headless_depth2_e6c7.json)は、画像認識やOS実入力ではない別のCore検証である。Python Random(seed)はBrowserのLCGと異なり、同seedでもtrajectoryや得点を両backend間で同一視しない。旧eb59のdepth1 fixtureとも別測定として扱い、本認識器の実画面精度へ加算しない。


後続のWindows評価profileでは、同frameのSCORE panelを局所Otsu・4近傍成分で分離し、bilinear3倍→二値化→白余白でPSM6/7の可読性を確保する。[9実header×2geometry、元4桁と独立6桁glyph、費用の記録](../validation/planning_score_panel_ocr.json)は旧helper0/18から新版18/18を確認する。confidence0.65・二幅一致・厳密parserを維持し、padding前のsource支持辺への接触、paddingを差し引いたOCR bboxの端切れ、同一cropの二重計数を拒否する。SCOREとBESTを分離し、Source/Planner入力や因果Outcomeへ戻さない。これは保存画素での評価OCR再生で、旧Windows試行の失敗や5ed5/e6c7の固定source実測を書換えず、新headの実OS受入れを別途要求する。


Windows5.5.3で0画像のOCR回帰が残ったため、次候補は先にSCORE panel内の全foregroundを元支持辺へ照合し、union＋小marginと同panel内拡幅の実2ROIを読む。[次候補・engine/model診断の記録](../validation/planning_score_foreground_union.json)を参照する。先頭桁・偏心label・多桁の支持を保持し、clamp後同一ROIはunknownにする。PSM7/.65を維持し、PSM7/8/10/13 raw probeは診断だけで、値補完や学習へ使わない。Linuxで0 confidenceは依然.728であり、Win改善は未確認のまま新CIで再検証する。


後続の機能親`175af6be54b8c9aa02d6aa8f83a41aaba05ff52b`は、実Win128条件の根拠に基づき、binary共通2ROIが空でpairを完成できない場合だけ同support/同2ROIのgrayscale・PSM7無whitelistを評価用に再読する。binary部分値とgrayの矛盾、既知の幅不一致/曖昧/低conf/切断はunknownに保ち、.65や通常入力OCRは変更しない。[exact Win RGB/TSV replayとLinux18例・4/6桁・費用](../validation/planning_score_gray_fallback.json)を保持する。これは新Win実行の代用ではなく、旧919/ba195の失敗は歴史として保全する。モデルの追加、値/seed固有処理、Source/学習への接続はない。


機能source `deec3e5cb1b26cc432269b1473432d94b0b3053c` の新Windows比較はpush/PR各3seed、全6job・18episodeが成功し、0を含む同画素21testsも全jobで通過した。216attemptのうち213実送信と213盤面変化、3holdで、PR seed481のPlannerだけ9入力/3hold、他17episodeは12入力だった。全得点を取得し、Stop/旧世代操作は全拒否。これは短比較であり、予測盤面の完全一致・長期生存・全ゲーム精度の証拠にはしない。600秒gateは旧launcherの初回全空盤面をreadyとするassertで未開始のまま失敗し、test-only修正後の新headで全CIを再検証する。


次の機能親`d4e15a1ba5fb28f8c7b81a4ba8239056fac61c2c`は、旧launcherの元15秒deadline/4候補期待を保ったready判定修正と、短比較専用SAFE_IDLE preflightを追加する。元の絶対45秒、5秒age、元token/Stop・所有照合を緩めず、通常600秒Campaignのobserve経路は維持する。deec3の成功や旧失敗を新phaseの実証へ置換せず、新Win CIの全gatesを確認する。

## 一時IPC改善との統合と初期空画面の区別

機能親`37bbafb1975e79fca7307dd621b61680403a91de`を通常mergeし、発光Recognizer・得点対応関数・元fixtureを保った。一時planning request/ready/checkpoint/finishedだけの単一encode・flush-close・atomic publishと有界raw再利用は、[親の費用・条件](../validation/planning_disposable_ipc_cost.json)で追跡する。一般Store・lease・Stop・Journalの永続化、400ms等の元予算、TTL、認識閾値は維持する。この親を取り込んだheadのWindows成功は、過去cohortから引き継がず新しい両event CIで確認する。

8dcの[初期空画面・lease失効の記録](../validation/planning_windows_8dc_failure_diagnostics.json)は元失敗として残す。復元された通常600秒episode2の画像は16セル背景だけで数字tileがなく、この画素はセル分類誤読を支持しない。GameManagerの初期RAF、actuatorのRAF、描画・取得面のどこで止まったかは未確定であり、659の元256発光によるROI拡大とは同一視しない。後の294は両600秒Campaignに成功したが、[PR480 Plannerの12保留/実入力0](../validation/planning_windows_294_remaining_failure.json)を失敗として保持する。失敗frame保全後だけのCDP/heartbeat診断は通常OFFで、観測への影響を明記し、Source・AI・cache・候補生成へ流入させない。ブラウザーのbackground/occlusion引数を変更した事実はない。

後続機能親`18e00e6b81cf1825a49e295513146170f6fde730`は、[私有HTTP accept queue](reference_http_accept_queue.md)の有界改善と、失敗後の固定11資源ResourceTiming/Grid/Tile constructor診断・[開発用10fault再現tool](reference_browser_post_failure_diagnostics.md)を追加する。これは認識code/閾値の変更ではない。[37bbの全18試行と原PNG](../validation/planning_windows_37bb.json)を保持し、HTMLActuator不在・3script応答欠落と初期空画素は、元glow誤読から分離する。隔離Linuxのloader faultで同じ見た目を再現しても、元Windowsのnetwork errorやqueueが原因だったと断定しない。新headの両event CIを別に要求し、入力・retry・flags・TTL・lease・unknown拒否を変更しない。
