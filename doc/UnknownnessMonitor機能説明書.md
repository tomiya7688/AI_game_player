# Unknownness Monitorの機能説明書

## この説明書の対象と目的

この説明書は、画面認識・知識検索・候補評価などの検出器を追加する実装者が、検出器の不確かさを`UnknownnessMonitor`へ渡し、scope別の瞬間判定と持続判定を確認するための説明です。Unknownnessは、現在の観測だけではゲーム状態や適切な判断を十分に特定できない状態を表します。

Issue #39は、不確かさの複数根拠を時系列で融合するコアAPIを追加します。このIssueのAPIはゲーム実行pipelineへまだ自動接続されていません。利用側が検出器の出力を正規化して`UnknownnessEvidence`を作り、`UnknownnessMonitor.evaluate()`を呼び出す必要があります。

## この機能が扱う範囲

`UnknownnessMonitor`は、各検出器が作った根拠を処理範囲ごとに集約し、今回だけの不確かさと複数観測で持続した不確かさを分けて返します。`UnknownnessMonitor`は、ACT・EXPLORE・OBSERVE_MORE・ABSTAIN・STOPなどの次の行動を選びません。unknown時の行動方針は別Issue #206の対象です。

根拠の原因は、認識確信度の低さ、既知prototypeとの距離、知識検索miss、評価器間の不一致、状態遷移の曖昧さです。LLMやProviderが自分で申告したconfidenceだけをunknown判定の根拠にしてはいけません。検出器が作った観測可能な値と出所を渡してください。`UnknownnessMonitor`はsignalの出所が独立しているか検証しないため、呼び出し側がLLM自身のconfidenceと別の検出器signalを併用してください。

`UnknownnessScope`は、根拠が影響する範囲を`perceptual`、`semantic`、`action`、`transition`、`knowledge`の5種類に分けます。範囲ごとに別々に集約するため、認識範囲の高いunknown scoreが、action範囲の低いscoreによって平均で薄まることはありません。

## 根拠の入力契約

各検出器は1つの根拠を`UnknownnessEvidence`で表します。`unknown_score`は不確かさの強さ、`evidence_confidence`はその根拠を検出器がどれだけ確かに測定できたか、`weight`は同じscope内で他の根拠と比べた相対的な重要度です。`source`には検出器名や保存知識の識別名を、`explanation`には観測内容を記録します。

検出器は、元の値を0から1の`unknown_score`に変換してから渡します。たとえば認識確信度`recognition_confidence`を不確かさへ変換する場合、`unknown_score = 1 - recognition_confidence`とします。prototype距離は検出器側で定義済みの距離尺度に基づいて0から1へ正規化してください。生の距離値を正規化せず渡してはいけません。検索missの有無を二値で表す検出器は、missなしを0、missありを1として渡せます。

`signal_id`は1回の画面観測内で根拠を識別するIDです。同じ観測でIDを重複させてはいけません。`observation_id`は評価呼び出しごとに変えてください。連続する2回の評価で同じ`observation_id`を使うと、`ValueError`になります。モニターは過去の全IDを保存しないため、呼び出し側が以前の観測IDも再利用しないように管理してください。

次の例では、認識確信度0.2から不確かさscore 0.8を作り、認識検出器の出所を記録します。

```python
from ai_game_player.unknownness import (
    UnknownnessEvidence,
    UnknownnessMonitor,
    UnknownnessScope,
    UnknownnessType,
)

monitor = UnknownnessMonitor()
recognition_confidence = 0.2
evidence = UnknownnessEvidence(
    signal_id="frame-104:recognition",
    unknown_type=UnknownnessType.LOW_RECOGNITION_CONFIDENCE,
    scope=UnknownnessScope.PERCEPTUAL,
    unknown_score=1.0 - recognition_confidence,
    evidence_confidence=0.95,
    weight=1.0,
    source="screen-recognizer/v2",
    explanation="frame-104で操作対象を示す認識確信度が0.2だった",
)
result = monitor.evaluate("frame-104", (evidence,))
print(result.instant_scopes)
```

## 複数根拠の融合方法

`UnknownnessMonitor`は同じscopeの根拠に対して、`weight × evidence_confidence`を実効重みとしてscoreを加重平均します。scope内のscoreを`uᵢ`、相対weightを`wᵢ`、根拠確信度を`cᵢ`とすると、融合scoreは次の式です。

```text
fused_score = Σ(uᵢ × wᵢ × cᵢ) / Σ(wᵢ × cᵢ)
```

融合結果の`confidence`は、scope内の根拠確信度を相対weightで加重平均した値です。これは校正済み確率ではなく、根拠の測定品質を0から1で示す値です。根拠がないscope、またはすべての根拠確信度が0のscopeでは、`score`を`None`、`confidence`を0にします。

集約時に根拠はscopeをまたぎません。たとえばperceptual scopeにscore 0.9、action scopeにscore 0.1がある場合、2値の全体平均0.5は作らず、それぞれの範囲の値と判定を返します。

## 瞬間判定と持続判定

既定のunknown閾値は0.65、解除閾値は0.35です。scopeの融合scoreが0.65以上なら、その観測の`instant_unknown`をTrueにします。1回だけの異常は瞬間判定に残り、既定では2回連続してunknown閾値以上になった場合に`persistent_unknown`をTrueにします。

持続状態は、融合scoreが0.35以下の観測が既定で2回連続した場合に解除します。0.35より大きく0.65より小さい中間score、根拠がない観測、確信度0の根拠は解除観測として数えません。unknownが保持中に根拠が欠落した場合も、根拠欠落だけでは持続状態を解除しません。

`UnknownnessMonitor`の閾値と必要な連続観測回数は、初期化時に変更できます。`clear_threshold`は`unknown_threshold`より小さくし、両方の閾値は0から1に設定してください。連続観測回数には1以上の整数を指定してください。

```python
monitor = UnknownnessMonitor(
    unknown_threshold=0.75,
    clear_threshold=0.25,
    confirm_observations=3,
    recovery_observations=2,
)
```

`UnknownnessMonitor.reset()`はすべてのscopeの持続状態と直前の観測IDを消去します。新しいゲームまたはSessionで同じmonitorを再利用する前に呼び出してください。1つのmonitorを複数ゲームで共有したり、複数スレッドから同時に呼び出したりしてはいけません。monitorは可変状態を持ち、並行呼び出しを同期する機能がありません。

## 結果の読み方と保存

`UnknownnessResult`は、5つすべてのscopeについて`UnknownnessScopeAssessment`を返します。`score is None`のscopeには利用可能な根拠がなく、`instant_unknown`はFalseです。`instant_types`と`persistent_types`は判定を支えた原因、`evidence`と`persistent_evidence`は根拠の値・確信度・出所・説明を示します。

全体の`instant_unknown`は、いずれかのscopeが今回unknownならTrueです。全体の`persistent_unknown`は、いずれかのscopeが持続unknownならTrueです。どのscopeが該当するかは`instant_scopes`と`persistent_scopes`で確認してください。JSON保存が必要な呼び出し側は`UnknownnessResult.to_dict()`を使います。この辞書のschemaは`unknownness/v1`です。

## 制約と確認方法

このAPIは検出器の出力を融合しますが、検出器自身の品質やscore正規化の妥当性を検証しません。prototype距離から0から1へ変換する方法、各根拠の重み、確信度の校正方法は各検出器の責務です。score 0.65はこのモニターの設定上の閾値であり、実ゲームで65%の確率でunknownになることを意味しません。

このIssueで確認する自動テストは`tests/test_unknownness.py`です。テストは根拠入力の検証、重み付き融合、scope分離、瞬間と持続の区別、2回連続でのラッチと解除、根拠欠落時の保持、型とJSON形式を確認します。ゲーム上の認識器へ信号を接続した動作やゲーム内の閾値妥当性は、このテストで確認していません。
