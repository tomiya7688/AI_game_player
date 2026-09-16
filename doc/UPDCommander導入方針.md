# UPD Commander 導入方針と品質Checker

## 目的

`upd-commander-base-design` の UI / Process / Data、Commander / Messenger / Processing の責務分離を AI Game Player へ段階導入する。

参照設計:

- repository: `tomiya7688/upd-commander-base-design`
- checker pin: `8abe0828e173e62ad42f69cfcfa002b6bf73c3b9`

最優先条件は、設計導入やcheckerがゲーム本体の実行速度ボトルネックにならないことである。そのため、既存hot pathを一括で多層ラッパーへ移動することはしない。

## 導入原則

1. 新規の横断機能からUPD構造を適用する。
2. 既存hot pathは、性能計測なしにCommander/Messenger層を追加しない。
3. UPD静的checker、バグchecker、性能checkerはゲーム実行中には起動しない。
4. checkerはCI・開発完了時に実行する。
5. Runtime側で層境界を増やす場合は、変更前後の性能checker結果を比較する。
6. Commanderは処理を選択・呼出するだけとし、計算・JSON保存・AST解析・benchmark計測を持たない。
7. Messengerは層間通信だけを担当する。
8. 実処理はProcessingへ置く。

## 現在の品質Application

品質診断機能は `src/ai_game_player/applications/quality/` に置く。

```text
applications/quality/
├─ process/
│  ├─ quality_commander.py
│  ├─ quality_messenger.py
│  ├─ performance_processing.py
│  └─ bug_processing.py
└─ data/
   ├─ quality_messenger.py
   ├─ quality_commander.py
   └─ report_processing.py
```

処理フロー:

```text
tools/*_check.py
  -> Process Commander
  -> Process Processing
  -> Process Messenger
  -> Data Messenger
  -> Data Commander
  -> Data Processing
  -> JSON report
```

`ai_game_player.app`、`DecisionPipeline`、`GamePlayerEngine` など通常ゲーム起動経路からこのquality Applicationをimportしない。したがってchecker導入による通常プレイ時のAST走査・benchmark・追加ファイルIOは0である。

## 既存本体の段階移行

既存コードは一括rename/refactorしない。現時点では次の責務対応として扱い、変更対象になったモジュールから順に責務を小さくする。

- UI: `app.py` と表示・ユーザー操作固有処理
- Process: Recognition / Evaluation / Decision / Safety / Execution orchestration
- Data: Config / Knowledge / History / Trace / Audit persistence
- Contract: `models.py` 等の層間で共有する値

既存flat moduleへ形式的なwrapperを足すだけの変更は禁止する。call depthを増やす価値が、責務分離・テスト容易性・性能の実測で確認できる場合だけ移行する。

## UPD Commander checker

専用workflow:

```text
.github/workflows/upd-architecture-ci.yml
```

Python checkerはUPDリポジトリのcommit SHAへ固定してinstallする。main追従ではなくpinすることで、外部checker更新による突然のCI変化を防ぐ。

CIでは次を行う。

- 新しいquality Application全体を走査
- Commander / Messengerファイルはwarningも失敗扱いで厳格検査
- `src/ai_game_player` 全体を走査して既存コードの移行候補をレポート
- レポートをartifact保存

全体走査では既存warningを即座にすべてblockしない。新規UPD境界は厳格にし、legacyは段階移行する。

## 実行速度checker

入口:

```text
python tools/performance_check.py
```

budget:

```text
config/performance_budgets.json
```

専用workflow:

```text
.github/workflows/performance-ci.yml
```

現在の測定対象:

- `FrameAnalyzer`
- `CandidateMerger`
- `DecisionContextBuilder`
- `SafetyGuard`

各caseはwarm-up後に複数batchを測定し、最も遅いbatchの1 operation平均をbudgetと比較する。単発値ではなくbatch測定にしてrunner jitterの影響を小さくする。

初期budgetは意図的に広めであり、微小差ではなく次を検出する。

- 桁違いの速度低下
- 不要な重処理のhot path流入
- O(n)からO(n^2)等への悪化
- checker/診断処理を誤ってruntime経路へ組み込んだ場合の大幅回帰

Windows CIはrunner差を考慮しbudget multiplierを使用する。基準実測が蓄積したらbudgetを段階的に縮める。

## バグchecker

入口:

```text
python tools/bug_check.py src tools tests
```

専用workflow:

```text
.github/workflows/bug-ci.yml
```

高確度で機械判定できる項目だけをblockingとする。

- `BUG001`: Python source parse failure
- `BUG101`: mutable default argument
- `BUG102`: `finally` 内returnによる例外抑制
- `BUG103`: duplicate constant dict key
- `BUG104`: broad exception handler後の到達不能handler
- `BUG105`: singletonではないliteralへの `is` / `is not`
- `BUG201`: 同一scopeのduplicate function/class definition

加えて `compileall` と既存unit testsを併用する。静的に断定しづらい挙動を「バグ」として大量に警告する方式にはしない。

## ローカル完了Gate

`finish_task.bat` は従来の生成doc / unit test / compile / diff checkに加えて次を実行する。

```text
bug_check.py
performance_check.py
```

UPD checkerは外部packageを必要とするためローカル必須dependencyにはせず、CIを正規Gateとする。これにより通常の開発環境・エンドユーザーbundleへUPD checker依存を持ち込まない。

## 性能上の禁止事項

- 毎frameでUPD checkerを実行しない。
- 毎Actionでbug checkerを実行しない。
- performance checkerを通常game loopから呼ばない。
- checker結果を得るためにRecognition/Decisionの入力を追加copyしない。
- 診断用JSON生成をgame loopへ追加しない。
- UPD準拠だけを目的にhot pathへ細粒度Messengerを大量追加しない。

設計遵守と実行性能が衝突する場合は、まず処理境界を粗粒度に保ち、性能測定結果をIssue/PRへ残してから分割方法を決める。
