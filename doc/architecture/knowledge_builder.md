# Experienceから操作効果を数値Knowledgeへ増分集計する

この文書は、モデルの再学習を行わない環境でもプレイ経験を次の判断へ使うための
開発者向け説明です。Issue #530の成果物は、検証済みのExperience eventから
昇格前のKnowledge候補を生成するBuilderです。通常利用者の追加操作はありません。

## 処理の境界

```text
実行済み操作のreceipt + 操作効果の検証根拠
  → canonical ExperienceEvent（knowledge.action_outcome）
  → source/操作世代で重複・相関sampleを排除
  → 分類件数 / Beta推定 / EWMA / 頻度表
  → 不変のKnowledgeSnapshot候補
  → #532の評価・昇格
  → #531のStep固定Adapter
  → 既存の判断・Verifier・Safety
```

Snapshotと保存の契約は[Knowledge Snapshot](knowledge_snapshot.md)、
親の学習方針はIssue #419です。Builderは既存の`ExperienceEvent`を入力に使い、
Reader、Dataset Builder、Artifact Store、モデルTrainerを作り直しません。
ReaderやSession側で新しいeventのbatchを選び、`KnowledgeBuilder.build`へ渡してください。
Builder自身はDBや履歴ファイルを開かず、モデル、GPU、ネットワーク、OS入力を使いません。

## 無意味な操作と未知結果を分ける

得点表示へのクリックも探索行動になり得ます。`no_effect`は、その操作が効かなかったと
外部の検証器が確認した場合のsoft evidenceです。操作を禁止するSafety判定ではありません。
失敗とは別に件数を保持し、既定の報酬は`-0.25`です。失敗の報酬は`-1`です。

画面のアニメーションや、ゲーム側で受動的に増えた得点だけでは、そのクリックの効用を
判断できません。`pixel_change`、`score_change`、`model_prediction`の自己申告は
`unknown`へ分類します。未知結果は件数と不確かさに残しますが、Beta推定、報酬、
候補prior、遷移表、校正値、数値hintを変更しません。

既知の効果として扱うには、次の全条件を満たす必要があります。

- `execution_status=executed`で、実行receiptの`action_generation`が相関系列の順序と一致する。
- 効果の`verified`がtrueで、eventとprovenanceの確信度の小さい方が既定0.6以上である。
- `evidence_kind`と`provenance.source`が、登録済みの検証元に含まれる。
- provenanceに元の効果証拠の`evidence_event_id`がある。

既定の検証元は`human_correction`、`deterministic_action_effect`、
`calibrated_action_effect`です。これらはgame/OCRの文字列やモデルが自由に設定する項目ではなく、
信頼されたruntimeの検証器が構成するmetadataです。型の一致だけで検証器自体を認証する仕組みではありません。

`dry_run`、`planned`、`cancelled`、`before_dispatch_failure`は学習対象から除外します。
送信後に結果が不明な`dispatch_unknown`は、既知の成功や失敗にせずunknown件数だけに残します。
実行された事実を確認できない判断を、次の画面へ勝手に帰属させないでください。

## 採用した既存手法

| 要求された方法 | 実装 | 効果と制限 |
|---|---|---|
| count / frequency | actionごとのsuccess/no_effect/failure/unknown件数、前状態×操作×次状態の頻度表 | 状態変化を操作効果と確認できた証拠だけを遷移表に入れる |
| Beta-Bernoulli相当 | 初期`Beta(1,1)`、生産的な効果でalpha、no-effect/failureでbetaへ確信度を加算 | 少数証拠で確定しない。未知結果は両方に加算しない |
| EWMA（指数移動平均） | 報酬と候補reliabilityを既定率0.1×証拠確信度で更新 | 一件で過去値を置き換えない |
| 縮小推定 | posterior差分を`known_count/(known_count+8)`で0へ縮める | 一件目のprior変化は約0.037以下（確信度1の場合） |
| bounded update | priorと重み・閾値の一証拠変化を既定0.1以下にする | Knowledgeはsoft evidenceのみ。Safetyや許可候補は変更しない |
| correlation control | sourceと操作系列の単調watermarkをSnapshotへ保存 | 同操作の複数frame・検出器を独立な成功試行として数えない |
| uncertainty | Beta事後分散とunknown比率から0〜1の不確かさを生成 | unknownが多い状態を確信済みにしない |
| provenance | source範囲、採用件数、hash chain、規則版と方針fingerprint | 元の自由文・画面・秘密をSnapshotへコピーしない |

`reward_mean`の分母は既知効果の件数です。unknownが挟まっても、既知の成功平均を0へ薄めません。
`update_rule=beta-ewma`、`update_rule_version=1.<policy fingerprint>`を記録します。
fingerprintは方針の数値・保持上限・信頼元を含みます。設定自体を再現する呼出側は、
利用した`KnowledgeBuilderPolicy`を実行ProfileやJournalへ記録してください。

## 入力と増分API

```python
result = builder.build(
    previous_snapshot, new_events,
    scope=scope, version="candidate.2",
    created_at="2026-11-01T12:00:00Z",
)
candidate = result.snapshot
```

一つの`knowledge.action_outcome` payloadの例です。通常のExperience外側には
event ID、時刻、source、provenanceを付けます。

```json
{
  "scope": {"game_id":"example","scene_id":"playing","profile_id":"default","player_id":"player.one","lineage_id":"lineage.one"},
  "source_id":"session.one","source_sequence":12,
  "correlation_id":"actions.one","correlation_sequence":7,"action_generation":7,
  "execution_status":"executed","action_id":"score-label",
  "outcome":"no_effect","evidence_kind":"deterministic_action_effect",
  "verified":true,"confidence":0.9
}
```

任意の数値入力は`candidate_reliability`、両方を指定した`from_state/to_state`、
schemaで宣言した`weight_hints`、`threshold_hints`、正規化xywhの`roi_hints`です。
重みの新しいhintは宣言済みの既定値から、ROIは全画面矩形から、制限した割合で更新します。
未知のキー、Safety設定、未加工画面、自由文は拒否します。

source IDは読み直すReaderの名前ではなく、同じ経験を同じIDで識別できるsession/episode/stream IDです。
source順序はそのID内で単調増加させてください。同じ操作に相関する検出器やframeは
同じ相関IDと操作世代を使ってください。Builderは最終的に判定した効果eventを受ける前提です。
先に暫定結果を同じ世代で送ると、後の訂正を重複として扱います。訂正を別の学習証拠とする
場合は、呼出側で検証元と相関の意味を明示してください。

Snapshot再ロード後もwatermark以下の再投入は数えません。古い順序が後から到着した場合も
保守的に重複扱いにします。欠けた過去sampleを自動で履歴全走査して埋める処理はありません。
証拠なし・全重複なら元Snapshotをそのまま返し、初回は`None`でstatic fallbackを維持します。
別game/scene/profile/Player系統、保持期限変更、不正な版、容量超過は失敗し、元Snapshotを壊しません。

## 資源と保存の境界

既定では一batch 256 event、action 256、source/correlation系列各128、遷移1024項目までです。
各上限はschemaの最大値以下で明示設定できます。上限に達したときは、重複履歴を忘れて
過去sampleを再び数える方式へ切り替えず、更新を拒否します。Gameplayを継続する方針と
新しいKnowledge世代への移行は#422/#532の制御側で扱います。

更新の計算量は選択済みbatchと、上限内のSnapshot統計に依存します。遷移の正規化は
そのboundedな遷移表を読みます。Snapshotの独立コピーを一つ作るため、巨大履歴の全走査が
なくても無コストではありません。#422のworker/control pathで呼び、Decisionのcritical pathへ
履歴処理や重い更新を持ち込まないでください。

実装言語はPythonです。既存Experience/SnapshotのJSON契約を同一process内で処理し、
呼出単位はframeごとの小計算ではなくbatchです。初回64 eventのp95は3.50msで、OS API、
GPU、モデル依存はありません。この段階でeventごとのFFIを増やす利益は確認していません。
将来の規模で計算が支配的になった場合は、JSON変換・コピー・配布の費用を含めた
batch単位のNative境界を測ってから配置を再評価してください。

sensitive artifact参照を持つ経験は既定で除外します。明示設定で集計を許可した場合も、
Snapshotへ保存するのは数値と不透明ID/hashだけです。既存の利用期限を更新で延長しません。
評価・昇格・Rollbackと本番pointerの変更はBuilderの仕事ではなく、#532の対象です。

## 検証と再現

開発者はリポジトリ直下で次を実行してください。

```sh
PYTHONPATH=src:. python -m unittest discover -s tests -p test_knowledge_builder.py -v
PYTHONPATH=src:. python tools/benchmark_knowledge_builder.py
```

seed 530のExperience stream、batch分割、単発/outlier、empty、重複、再ロード後再投入、
相関frame、実行世代、実行前失敗、未知の効果、scope不一致、数値hint、Safety override拒否を検証します。
同じstreamを一batchと分割batchにした場合に統計・prior・出所が一致することも確認します。

初回のクラウドLinux/Python 3.12.14測定は64 event×200反復です。
新規更新p50/p95は1.90/3.50ms、tracemalloc peak 32,420 bytes、重複batchは1.24/1.37msで
元Snapshot objectを維持しました。環境依存の観測であり回帰閾値ではありません。
測定は`build/knowledge-builder/report.json`へ出力し、CPU時間、Snapshot bytes、保持/peakの
traced allocationを分けて記録します。tracemalloc値をプロセス全体のRSSと混同しません。

Coreは数値Knowledge候補の生成です。Runtimeの自動更新は#422、判断への反映は#531、
評価・昇格は#532に分けます。このBuilderの単体成功だけで、実ゲームの有効行動が増えたとは扱いません。
