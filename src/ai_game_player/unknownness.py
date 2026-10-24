from dataclasses import dataclass
from enum import Enum
from math import fsum, isfinite
from typing import Iterable


DEFAULT_UNKNOWN_THRESHOLD = 0.65
DEFAULT_CLEAR_THRESHOLD = 0.35
DEFAULT_CONFIRM_OBSERVATIONS = 2
DEFAULT_RECOVERY_OBSERVATIONS = 2


# {
#   責務: [UnknownnessType: 判断の不確かさを示す観測原因を、保存・集計で共通の名前にする]
#   値: [認識確信度の低さ、既知prototypeとの距離、知識検索の不一致、評価器間の意見不一致、状態遷移の曖昧さを区別する]
#   制約: [Providerの自己申告confidenceだけではUnknownを確定しない。呼び出し側は検出器が観測した別signalと併用する]
# }
class UnknownnessType(str, Enum):
    LOW_RECOGNITION_CONFIDENCE = "low_recognition_confidence"
    PROTOTYPE_DISTANCE = "prototype_distance"
    RETRIEVAL_MISS = "retrieval_miss"
    EVALUATOR_DISAGREEMENT = "evaluator_disagreement"
    TRANSITION_AMBIGUITY = "transition_ambiguity"


# {
#   責務: [UnknownnessScope: 不確かさが影響するゲーム処理の範囲を識別する]
#   値: [perceptualは画面認識、semanticは意味解釈、actionは操作候補、transitionは状態遷移、knowledgeは保存知識の検索を示す]
# }
class UnknownnessScope(str, Enum):
    PERCEPTUAL = "perceptual"
    SEMANTIC = "semantic"
    ACTION = "action"
    TRANSITION = "transition"
    KNOWLEDGE = "knowledge"


@dataclass(frozen=True)
# {
#   責務: [UnknownnessEvidence: 1つの検出器が観測した不確かさの強さ・範囲・出所を保持する]
#   フィールド: [signal_id: 同じ観測内で根拠を識別するID, unknown_type: 不確かさが生じた観測原因, scope: 影響を受ける処理範囲, unknown_score: 根拠が示す不確かさを0（不確かさなし）から1（最大）で正規化した値, evidence_confidence: 検出器がこの根拠をどれだけ確かとみなすか, weight: 同じscopeの他根拠に対する相対重み, source: 数値を生成した検出器や保存知識の識別名, explanation: 数値が示す具体的な観測内容]
#   制約: [認識確信度は1から差し引いてunknown_scoreへ変換し、prototype距離は検出器側で0から1へ正規化する。Providerの自己申告confidenceだけでUnknownを判断しない]
# }
class UnknownnessEvidence:
    signal_id: str
    unknown_type: UnknownnessType
    scope: UnknownnessScope
    unknown_score: float
    evidence_confidence: float
    weight: float
    source: str
    explanation: str

    # {
    #   責務: [__post_init__: 不確かさの根拠が識別可能で、集計に使える数値範囲を満たすことを確認する]
    #   引数: []
    #   戻り値: [なし。根拠のID・範囲・原因・説明が不足する場合はValueErrorを送出する]
    #   エラー: [ValueError: 空の識別情報、未定義の原因・範囲、0から1の範囲外の数値、または正でない重みの場合]
    # }
    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.signal_id, self.source, self.explanation)):
            raise ValueError("unknownness evidence requires a signal ID, source, and explanation")
        if not isinstance(self.unknown_type, UnknownnessType):
            raise ValueError("unknownness evidence requires a supported unknownness type")
        if not isinstance(self.scope, UnknownnessScope):
            raise ValueError("unknownness evidence requires a supported scope")
        if not _is_unit_interval(self.unknown_score):
            raise ValueError("unknownness score must be between 0 and 1")
        if not _is_unit_interval(self.evidence_confidence):
            raise ValueError("evidence confidence must be between 0 and 1")
        if not _is_positive_finite_number(self.weight):
            raise ValueError("unknownness evidence weight must be finite and positive")

    # {
    #   責務: [to_dict: 不確かさの観測根拠をJSON保存と監査で使える辞書へ変換する]
    #   引数: []
    #   戻り値: [dict[str, str | float]: 原因・範囲・正規化値・確信度・重み・出所・説明を含むunknownness-evidence/v1形式]
    # }
    def to_dict(self) -> dict[str, str | float]:
        return {
            "schema": "unknownness-evidence/v1",
            "signal_id": self.signal_id,
            "unknown_type": self.unknown_type.value,
            "scope": self.scope.value,
            "unknown_score": float(self.unknown_score),
            "evidence_confidence": float(self.evidence_confidence),
            "weight": float(self.weight),
            "source": self.source,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
# {
#   責務: [UnknownnessScopeAssessment: 1つの処理範囲における今回の不確かさと、時間平滑化後の状態を分けて返す]
#   フィールド: [scope: 評価した処理範囲, score: 今回の観測を統合した不確かさ。使える根拠がなければNone, confidence: 今回の統合値を支える根拠の平均確信度, instant_unknown: 今回だけで閾値を超えたか, persistent_unknown: 連続観測後に保持中の不確かさか, instant_types: 今回の閾値超過を支えた観測原因, persistent_types: 持続状態を最後に支えた観測原因, evidence: 今回の観測根拠, persistent_evidence: 持続状態を最後に支えた観測根拠]
# }
class UnknownnessScopeAssessment:
    scope: UnknownnessScope
    score: float | None
    confidence: float
    instant_unknown: bool
    persistent_unknown: bool
    instant_types: tuple[UnknownnessType, ...] = ()
    persistent_types: tuple[UnknownnessType, ...] = ()
    evidence: tuple[UnknownnessEvidence, ...] = ()
    persistent_evidence: tuple[UnknownnessEvidence, ...] = ()

    # {
    #   責務: [__post_init__: 範囲評価のscope・集計値・状態・原因・根拠が互いに整合することを確認する]
    #   引数: []
    #   戻り値: [なし。範囲、数値、状態型、根拠のscopeが契約を満たさない場合はValueErrorを送出する]
    #   エラー: [ValueError: 未定義scope、範囲外score/confidence、bool以外の状態値、scope不一致の根拠がある場合]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.scope, UnknownnessScope):
            raise ValueError("unknownness assessment requires a supported scope")
        if not isinstance(self.instant_unknown, bool) or not isinstance(self.persistent_unknown, bool):
            raise ValueError("unknownness assessment states must be boolean")
        if self.score is not None and not _is_unit_interval(self.score):
            raise ValueError("unknownness assessment score must be between 0 and 1")
        if not _is_unit_interval(self.confidence):
            raise ValueError("unknownness assessment confidence must be between 0 and 1")
        if not isinstance(self.instant_types, tuple) or any(
            not isinstance(value, UnknownnessType) for value in self.instant_types
        ):
            raise ValueError("instant unknownness causes must be a tuple of supported types")
        if not isinstance(self.persistent_types, tuple) or any(
            not isinstance(value, UnknownnessType) for value in self.persistent_types
        ):
            raise ValueError("persistent unknownness causes must be a tuple of supported types")
        if not isinstance(self.evidence, tuple) or any(
            not isinstance(value, UnknownnessEvidence) or value.scope is not self.scope
            for value in self.evidence
        ):
            raise ValueError("scope evidence must contain evidence for the same scope")
        if not isinstance(self.persistent_evidence, tuple) or any(
            not isinstance(value, UnknownnessEvidence) or value.scope is not self.scope
            for value in self.persistent_evidence
        ):
            raise ValueError("persistent evidence must contain evidence for the same scope")
        if self.score is None and self.confidence != 0.0:
            raise ValueError("unknownness confidence must be zero when no usable score exists")

    # {
    #   責務: [to_dict: scopeごとの瞬間判定・持続判定と根拠をJSON形式へ変換する]
    #   引数: []
    #   戻り値: [dict[str, object]: scoreがNoneなら信頼できる根拠がないことを示す範囲評価辞書]
    # }
    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope.value,
            "score": self.score,
            "confidence": self.confidence,
            "instant_unknown": self.instant_unknown,
            "persistent_unknown": self.persistent_unknown,
            "instant_types": [value.value for value in self.instant_types],
            "persistent_types": [value.value for value in self.persistent_types],
            "evidence": [value.to_dict() for value in self.evidence],
            "persistent_evidence": [value.to_dict() for value in self.persistent_evidence],
        }


@dataclass(frozen=True)
# {
#   責務: [UnknownnessResult: 1つの画面観測について全scopeの瞬間判定・持続判定・根拠をまとめる]
#   フィールド: [observation_id: 判定を作った画面観測の一意なID, scopes: perceptual・semantic・action・transition・knowledgeの範囲別評価]
#   集約規則: [全範囲のどれかがinstant_unknownならinstant_unknown、どれかがpersistent_unknownならpersistent_unknownとする。範囲別のscoreを全体平均で薄めない]
# }
class UnknownnessResult:
    observation_id: str
    scopes: tuple[UnknownnessScopeAssessment, ...]

    # {
    #   責務: [__post_init__: 同じ観測の結果に全scopeの評価が1つずつ入ることを確かめる]
    #   引数: []
    #   戻り値: [なし。IDが空、scopeが重複・欠落、または範囲評価以外の値が含まれる場合はValueErrorを送出する]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, str) or not self.observation_id.strip():
            raise ValueError("unknownness result requires an observation ID")
        if not isinstance(self.scopes, tuple):
            raise ValueError("unknownness result scopes must be an immutable tuple")
        if any(not isinstance(value, UnknownnessScopeAssessment) for value in self.scopes):
            raise ValueError("unknownness result scopes must contain scope assessments")
        scope_values = [value.scope for value in self.scopes]
        if len(scope_values) != len(set(scope_values)):
            raise ValueError("unknownness result cannot contain duplicate scopes")
        if set(scope_values) != set(UnknownnessScope):
            raise ValueError("unknownness result must contain an assessment for every scope")

    # {
    #   責務: [instant_unknown: 今回の観測だけで不確かさの閾値を超えたscopeがあるか返す]
    #   引数: []
    #   戻り値: [bool: 1つ以上のscopeが今回のunknown閾値を超えた場合True]
    # }
    @property
    def instant_unknown(self) -> bool:
        return any(value.instant_unknown for value in self.scopes)

    # {
    #   責務: [persistent_unknown: 時間平滑化後も不確かさを保持しているscopeがあるか返す]
    #   引数: []
    #   戻り値: [bool: 1つ以上のscopeが持続unknown状態ならTrue]
    # }
    @property
    def persistent_unknown(self) -> bool:
        return any(value.persistent_unknown for value in self.scopes)

    # {
    #   責務: [instant_scopes: 今回の観測だけでunknown閾値を超えたscopeを返す]
    #   引数: []
    #   戻り値: [tuple[UnknownnessScope, ...]: 現在閾値を超えている範囲を定義順に返す]
    # }
    @property
    def instant_scopes(self) -> tuple[UnknownnessScope, ...]:
        return tuple(value.scope for value in self.scopes if value.instant_unknown)

    # {
    #   責務: [persistent_scopes: 時間平滑化でunknown状態を保持しているscopeを返す]
    #   引数: []
    #   戻り値: [tuple[UnknownnessScope, ...]: 持続unknown状態の範囲を定義順に返す]
    # }
    @property
    def persistent_scopes(self) -> tuple[UnknownnessScope, ...]:
        return tuple(value.scope for value in self.scopes if value.persistent_unknown)

    # {
    #   責務: [instant_types: 今回の観測でunknown閾値を超えたscopeに含まれる観測原因を返す]
    #   引数: []
    #   戻り値: [tuple[UnknownnessType, ...]: 重複を除き定義順に並べた今回の観測原因]
    # }
    @property
    def instant_types(self) -> tuple[UnknownnessType, ...]:
        present = {value for scope in self.scopes if scope.instant_unknown for value in scope.instant_types}
        return tuple(value for value in UnknownnessType if value in present)

    # {
    #   責務: [persistent_types: 現在保持中のunknown状態を支える観測原因を返す]
    #   引数: []
    #   戻り値: [tuple[UnknownnessType, ...]: 重複を除き定義順に並べた保持中の観測原因]
    # }
    @property
    def persistent_types(self) -> tuple[UnknownnessType, ...]:
        present = {value for scope in self.scopes if scope.persistent_unknown for value in scope.persistent_types}
        return tuple(value for value in UnknownnessType if value in present)

    # {
    #   責務: [to_dict: 全範囲の評価をunknownness/v1としてJSONへ保存できる辞書にする]
    #   引数: []
    #   戻り値: [dict[str, object]: 全体のinstant/persistent判定、範囲一覧、原因、現在と保持中の根拠]
    # }
    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "unknownness/v1",
            "observation_id": self.observation_id,
            "instant_unknown": self.instant_unknown,
            "persistent_unknown": self.persistent_unknown,
            "instant_scopes": [value.value for value in self.instant_scopes],
            "persistent_scopes": [value.value for value in self.persistent_scopes],
            "instant_types": [value.value for value in self.instant_types],
            "persistent_types": [value.value for value in self.persistent_types],
            "scopes": [value.to_dict() for value in self.scopes],
        }


@dataclass
# {
#   責務: [_ScopeHysteresisState: 1scope分の連続unknown確認回数と、解除待ち状態・保持中根拠を追跡する]
#   フィールド: [unknown_streak: unknown閾値を連続超過した観測数, clear_streak: clear閾値以下が連続した観測数, persistent_unknown: 持続unknownを保持中か, persistent_types: 持続状態を最後に支えた観測原因, persistent_evidence: 持続状態を最後に支えた根拠]
#   保持量: [scopeごとに1回分の根拠だけを保持し、長時間実行で履歴が増え続けない]
# }
class _ScopeHysteresisState:
    unknown_streak: int = 0
    clear_streak: int = 0
    persistent_unknown: bool = False
    persistent_types: tuple[UnknownnessType, ...] = ()
    persistent_evidence: tuple[UnknownnessEvidence, ...] = ()


# {
#   責務: [UnknownnessMonitor: 検出器の不確かさ根拠をscopeごとに重み付き融合し、観測回数によるヒステリシスを加える]
#   保持状態: [各scopeの連続unknown回数・解除回数・最後に確定した根拠。上限は5scope×今回の入力根拠件数]
#   非対象: [ACT・EXPLORE・OBSERVE_MORE・ABSTAIN・STOPなど、unknown発生後の行動方針は選ばない]
# }
class UnknownnessMonitor:
    # {
    #   責務: [__init__: unknown確定・解除のscore境界と、連続観測で必要な確認回数を設定する]
    #   引数: [unknown_threshold: instant_unknownとなるscore境界, clear_threshold: 持続unknownを解除可能とみなすscore境界, confirm_observations: persistent_unknownへ移るため必要な連続高score観測数, recovery_observations: persistent_unknownを解除するため必要な連続低score観測数]
    #   戻り値: []
    #   エラー: [ValueError: 閾値が0から1の範囲外、clear境界がunknown境界以上、または観測回数が正の整数でない場合]
    # }
    def __init__(
        self,
        unknown_threshold: float = DEFAULT_UNKNOWN_THRESHOLD,
        clear_threshold: float = DEFAULT_CLEAR_THRESHOLD,
        confirm_observations: int = DEFAULT_CONFIRM_OBSERVATIONS,
        recovery_observations: int = DEFAULT_RECOVERY_OBSERVATIONS,
    ) -> None:
        if not _is_unit_interval(unknown_threshold) or not _is_unit_interval(clear_threshold):
            raise ValueError("unknownness thresholds must be between 0 and 1")
        if clear_threshold >= unknown_threshold:
            raise ValueError("clear threshold must be lower than unknown threshold")
        if not _is_positive_integer(confirm_observations) or not _is_positive_integer(recovery_observations):
            raise ValueError("hysteresis observation counts must be positive integers")
        self.unknown_threshold = float(unknown_threshold)
        self.clear_threshold = float(clear_threshold)
        self.confirm_observations = confirm_observations
        self.recovery_observations = recovery_observations
        self._scope_states = {scope: _ScopeHysteresisState() for scope in UnknownnessScope}
        self._last_observation_id: str | None = None

    # {
    #   責務: [evaluate: 1回の画面観測に対応する根拠をscope別に融合し、瞬間判定と保持中状態を返す]
    #   処理: [1: 空ID・不正根拠・同一IDの連続再送・重複signal IDを検査する, 2: scope内でweight×evidence_confidenceを使ってscoreを平均する, 3: enter/clear閾値と連続観測数で各scopeのpersistent状態を更新する]
    #   引数: [observation_id: 1回の画面観測ごとに変わるID, evidence: 認識・prototype距離・検索・評価器などの正規化済み不確かさ根拠]
    #   戻り値: [UnknownnessResult: 5scopeの瞬間・持続状態と、現在および保持状態を支える根拠]
    #   エラー: [ValueError: observation IDが空・直前IDと重複、根拠型が不正、またはsignal IDが重複する場合]
    # }
    def evaluate(
        self,
        observation_id: str,
        evidence: Iterable[UnknownnessEvidence],
    ) -> UnknownnessResult:
        if not isinstance(observation_id, str) or not observation_id.strip():
            raise ValueError("unknownness evaluation requires an observation ID")
        if observation_id == self._last_observation_id:
            raise ValueError("observation ID must change for each unknownness evaluation")
        try:
            evidence_items = tuple(evidence)
        except TypeError as error:
            raise ValueError("unknownness evaluation evidence must be iterable") from error
        if any(not isinstance(item, UnknownnessEvidence) for item in evidence_items):
            raise ValueError("unknownness evaluation evidence must contain UnknownnessEvidence values")
        signal_ids = [item.signal_id for item in evidence_items]
        if len(signal_ids) != len(set(signal_ids)):
            raise ValueError("signal IDs must be unique within one observation")

        evidence_by_scope: dict[UnknownnessScope, list[UnknownnessEvidence]] = {
            scope: [] for scope in UnknownnessScope
        }
        for item in sorted(evidence_items, key=lambda value: (value.scope.value, value.signal_id)):
            evidence_by_scope[item.scope].append(item)

        assessments = tuple(
            self._assess_scope(scope, tuple(evidence_by_scope[scope]))
            for scope in UnknownnessScope
        )
        self._last_observation_id = observation_id
        return UnknownnessResult(observation_id, assessments)

    # {
    #   責務: [reset: 新しいgame/sessionの観測を始める前に、全scopeの時間平滑化状態を破棄する]
    #   引数: []
    #   戻り値: [なし。保持中unknown・根拠・直前観測IDを初期状態へ戻す]
    # }
    def reset(self) -> None:
        self._scope_states = {scope: _ScopeHysteresisState() for scope in UnknownnessScope}
        self._last_observation_id = None

    # {
    #   責務: [_assess_scope: scopeの根拠を重み付き融合し、instant判定とhysteresis状態を更新する]
    #   処理: [high境界超過が連続すればpersistentを開始し、low境界以下が連続すれば解除する。中間scoreや根拠欠落は解除回数に数えない]
    #   引数: [scope: 更新する処理範囲, evidence: 今回観測した同scopeの根拠]
    #   戻り値: [UnknownnessScopeAssessment: 今回のscore・確信度・瞬間判定・保持状態・根拠]
    # }
    def _assess_scope(
        self,
        scope: UnknownnessScope,
        evidence: tuple[UnknownnessEvidence, ...],
    ) -> UnknownnessScopeAssessment:
        state = self._scope_states[scope]
        score, confidence = _fuse_scope(evidence)
        instant_unknown = score is not None and score >= self.unknown_threshold

        if instant_unknown:
            state.unknown_streak = min(state.unknown_streak + 1, self.confirm_observations)
            state.clear_streak = 0
            if state.unknown_streak >= self.confirm_observations:
                state.persistent_unknown = True
                state.persistent_types = _evidence_types(evidence)
                state.persistent_evidence = evidence
        else:
            state.unknown_streak = 0
            if score is not None and score <= self.clear_threshold and state.persistent_unknown:
                state.clear_streak += 1
                if state.clear_streak >= self.recovery_observations:
                    state.persistent_unknown = False
                    state.persistent_types = ()
                    state.persistent_evidence = ()
            else:
                state.clear_streak = 0

        current_types = _evidence_types(evidence) if instant_unknown else ()
        return UnknownnessScopeAssessment(
            scope,
            score,
            confidence,
            instant_unknown,
            state.persistent_unknown,
            current_types,
            state.persistent_types if state.persistent_unknown else (),
            evidence,
            state.persistent_evidence if state.persistent_unknown else (),
        )


# {
#   責務: [_fuse_scope: 1scopeの根拠を重みと検出器確信度で融合し、正規化scoreと平均確信度を返す]
#   処理: [各重みを最大重みで割り、weight×evidence_confidenceをscoreの寄与に使う。全根拠の確信度が0なら有効scoreなしとする]
#   引数: [evidence: 同じscopeで今回観測した不確かさ根拠]
#   戻り値: [tuple[float | None, float]: 融合scoreと確信度。利用できる根拠がない場合(None, 0)を返す]
# }
def _fuse_scope(evidence: tuple[UnknownnessEvidence, ...]) -> tuple[float | None, float]:
    if not evidence:
        return None, 0.0
    maximum_weight = max(float(item.weight) for item in evidence)
    scaled_weights = [float(item.weight) / maximum_weight for item in evidence]
    base_weight = fsum(scaled_weights)
    effective_weights = [weight * float(item.evidence_confidence) for item, weight in zip(evidence, scaled_weights)]
    total_effective_weight = fsum(effective_weights)
    if total_effective_weight <= 0.0:
        return None, 0.0
    score = fsum(float(item.unknown_score) * weight for item, weight in zip(evidence, effective_weights))
    return round(score / total_effective_weight, 6), round(total_effective_weight / base_weight, 6)


# {
#   責務: [_evidence_types: unknown scoreへ正の寄与をした異なる観測原因を定義順で返す]
#   引数: [evidence: 同scope内で今回観測した不確かさ根拠]
#   戻り値: [tuple[UnknownnessType, ...]: 重複を除いてenum定義順に並べた原因]
# }
def _evidence_types(evidence: tuple[UnknownnessEvidence, ...]) -> tuple[UnknownnessType, ...]:
    present = {
        item.unknown_type
        for item in evidence
        if item.evidence_confidence > 0.0 and item.unknown_score > 0.0
    }
    return tuple(value for value in UnknownnessType if value in present)


# {
#   責務: [_is_unit_interval: 真偽値ではない有限数が0から1の範囲にあるか検査する]
#   引数: [value: 検査対象の値]
#   戻り値: [bool: 0から1の有限な数値ならTrue]
# }
def _is_unit_interval(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        normalized_value = float(value)
    except (OverflowError, ValueError):
        return False
    return isfinite(normalized_value) and 0.0 <= normalized_value <= 1.0


# {
#   責務: [_is_positive_finite_number: 真偽値ではない有限の正数か検査する]
#   引数: [value: 重みとして使う数値]
#   戻り値: [bool: floatへ安全に変換できる正の有限数ならTrue]
# }
def _is_positive_finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        normalized_value = float(value)
    except (OverflowError, ValueError):
        return False
    return isfinite(normalized_value) and normalized_value > 0.0


# {
#   責務: [_is_positive_integer: 真偽値ではない正の整数か検査する]
#   引数: [value: 観測回数の設定値]
#   戻り値: [bool: 1以上の整数ならTrue]
# }
def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
