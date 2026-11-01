# 2048起動失敗の診断保存

Issue #637は、自然なbrowser起動失敗の原因を調べるための証拠保存を追加する。親 #459の原因解消とは別の成果物である。既存 `tools/run_2048_campaign.py --save-failure-evidence` を明示した場合だけ有効で、既定はOFFのままとする。

## 背景と因果の区別

PR #631 push run `41307243065` / Windows job `124850039216` / HEAD `934facd` は495.488秒・305操作、terminal restart 2回後のepisode 3起動で失敗した。起動PID4336、`owned_titled_windows=3`、`other_2048_windows=0`、対象ページGETへの200/非200応答生成は共に0件だった。artifact `13085095035` が残る。先行する #459 コメント6488798802/6545716215にも同じ診断値の形があるが、同じ根本原因の証明ではない。

ログのHTTP handlerはrequestline待ち中にWinError10054を受けている。起動timeout後のbrowser killによる切断でも説明できるため、切断を起動失敗の原因と断定しない。GET応答0件は、要求未到達・browserの受信失敗・描画失敗を区別しない。既存のowned titled数も可視窓数ではない。認識失敗保存器は起動成功後に生成されるため、従来は起動失敗画面を取得する前にbrowserが閉じられていた。

## 保存契約

`ReferenceGame2048Episode` が既存flagから `StartupFailureEvidence` を接続する。`BrowserWindowSession.start()` は起動失敗を捕捉すると、processが生成済みならcleanup前に診断を試す。その後は成功・失敗にかかわらず既存のbrowser/profile/server cleanupへ進む。保存例外は型だけを併記し、元例外オブジェクトやerrno/filenameを置換しない。標準の文字列TimeoutErrorには既存HTTP診断とmanifest参照を付ける。

- 保存する窓は起動PIDを照合したものだけで、最大3件。可視窓を優先し、タイトルのない起動途中の窓も対象にする。矩形と可視状態、`game_title` / `browser_title` / `other_owned_title` / `untitled` の固定分類を残し、生タイトルを保存しない。
- 画素は取得前後に所有PIDを再照合したHWNDへの既存Native WGCで取得する。隠れた窓・不正な矩形・容量超過はmetadataだけを保存する。GDIによるdesktop矩形の代替を使わず、別アプリの重なりを証拠へ混ぜない。
- 窓あたりBGRA 8MiB、合計16MiB。失敗した書込みも容量を予約する。PNGは既存recognition evidence writerの無損失表示用表現、BGRAとそのSHA-256・寸法・取得時刻は完全な画素照合用である。全体は3窓、BGRA16MiBとそのRGB PNG表現で有界になる。
- Native WGCの任意 `max_frame_bytes` はframe metadataを取得してからPython画素allocation/copyの前に検査する。既定Noneは従来の契約を保持する。上限拒否でもframe leaseを解放し、正常なruntimeを破損扱いにしない。
- 診断を別processで行い、全体を5秒、WGC待ちを窓あたり400msに制限する。期限時は診断子processを終了して待ち、元browserを残さずcleanupする。途中でもatomic JSONの最後のmanifestを保持する。manifest読込は64KiBまで。
- `browser_spawn` / `window_wait` / `startup_total`、診断全体と窓別capture時間をミリ秒で分離する。25秒の起動成功判定を診断時間で延長しない。worker失敗/timeout/capture不可も固定statusまたは例外型で表す。

出力は既存campaign artifact配下の `episode-NNNN/startup-failure-*/manifest.json` と同じbundle内のPNG/BGRAである。通常観測や起動成功では診断を保存しない。診断窓を操作対象へ登録したり、PIDと2048 titleの一致条件を変更したりしない。600秒のcampaign受入れ、入力許可、停止・Safety基準は維持する。

診断中に遅れてゲーム画面が現れても、25秒の起動失敗を成功へ変更しない。取得時刻は起動待ちと診断で分離して解釈する。

## 配置と検証範囲

低頻度の失敗時I/Oとlifecycle制御は既存launcherと同じPython toolsへ置き、OS画素取得は既存C++ WGC/C ABIを再利用する。新しい画像処理・保存基盤・入力経路は作らない。stderr収集やHTTP health probeは今回追加せず、必要になったときに実証拠から判断する。

Linux fake回帰は既定OFF、保存→cleanup順序、元例外保持、worker期限、最大件数/矩形/画素上限、hash、所有PIDの前後再照合、保存/取得失敗、lease解放を検査する。全finish相当checkを実行する。Windowsでの自然起動失敗の実画面取得・原因特定はUnverifiedであり、診断成功をプレイ成功や#459の解決へ読み替えない。
