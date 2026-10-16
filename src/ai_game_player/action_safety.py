import json
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_game_player.journal_adapters import LegacyEventAdapter
from ai_game_player.models import ActionCandidate, ScreenObservation


class SafetyStatus(str, Enum):
    SAFE = "SAFE"
    SUSPICIOUS = "SUSPICIOUS"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class SafetyEvidence:
    check: str
    severity: str
    confidence: float
    message: str
    source: str = "action_safety_rule/v1"

    def __post_init__(self) -> None:
        if not self.check.strip() or not self.message.strip() or not self.source.strip():
            raise ValueError("safety evidence metadata must not be empty")
        if self.severity not in {"info", "warning", "block"}:
            raise ValueError("safety evidence severity must be info, warning, or block")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("safety evidence confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
# {
#   責務: [
#     SafetyEvaluationContext: 操作が危険かを調べるため、ゲーム目標・操作後の予測・候補選択の検査結果を評価器へ渡す
#   ]
#   フィールド: [
#     current_goal: 今回のプレイで最後に達成したいゲーム内の目的。危険を伴う操作が目的に必要かを評価器が判断する
#     short_term_goal: 次の数手で達成したい中間目的。最終目的だけでは操作の意図を決められない場合に使う
#     expected_effect_consistent: 操作後に観測した変化が、操作前に予測した変化と一致したか。未実行ならNone
#     utility_score: 候補が目的を進める見込みを-1から1で表す。-1は妨げる、0は影響なし、1は大きく進める
#     utility_confidence: utility_scoreの根拠の強さを0から1で表す。0は根拠なし、1は強い根拠がある
#     target_scope_hint: 操作の影響範囲の予測。localは画面の一部、gameはゲーム内、sessionは今回の実行、systemはOS全体
#     reversible_hint: 操作前の状態へ戻せる見込み。判断できない場合はNone
#     decision_reliability: 候補が許可一覧に含まれ、現在画面と理由に食い違いがないかを別の検査器が調べた結果
#   ]
# }
class SafetyEvaluationContext:
    current_goal: str = ""
    short_term_goal: str = ""
    expected_effect_consistent: bool | None = None
    utility_score: float | None = None
    utility_confidence: float | None = None
    target_scope_hint: str | None = None
    reversible_hint: bool | None = None
    decision_reliability: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.utility_score is not None and (not isfinite(self.utility_score) or not -1.0 <= self.utility_score <= 1.0):
            raise ValueError("utility score must be between -1 and 1")
        if self.utility_confidence is not None and (
            not isfinite(self.utility_confidence) or not 0.0 <= self.utility_confidence <= 1.0
        ):
            raise ValueError("utility confidence must be between 0 and 1")
        if self.target_scope_hint is not None and self.target_scope_hint not in {"local", "game", "session", "system"}:
            raise ValueError("target scope hint must be local, game, session, or system")


@dataclass(frozen=True)
# {
#   責務: [
#     ActionSafetyResult: 操作そのものの危険度判定と、選択候補が画面・許可一覧に合うかの検査結果を分けて保存する
#   ]
#   フィールド: [
#     assessment_id: この安全評価を後から特定し、実行結果やゲーム結果と結び付ける一意なID
#     action_id: 画面内で危険度を調べた操作候補のID
#     status: SAFEは既知の遮断条件なし、SUSPICIOUSは実行前の追加確認が必要、BLOCKは入力を送らない判定
#     recognition_confidence: 画面解析が対象を正しく見つけた見込み。低い認識確信度と操作の危険度は別々に扱う
#     safety_score: 安全側の点数。値が低いほど入力を許可しにくく、高いほど検査規則上の危険が少ない
#     risk_score: 危険側の点数。0は低リスク、1は規則上の最高リスク
#     risk_level: risk_scoreをlow（低）、medium（中）、high（高）、critical（極めて高い）の4段階に分けた区分
#     reversible: 実行後にゲーム内状態を元へ戻せると確認できた場合True。確認できない場合None
#     blast_radius: 誤操作や失敗が起きた場合に影響する範囲をlow（小）からcritical（極めて大）で示す
#     target_scope: 操作対象の範囲。localは画面の一部、gameはゲーム内、sessionは今回の実行、systemはOS全体
#     goal_alignment: 危険操作が現在の目標に必要と確認できた場合True。目標を照合できない場合None
#     expected_effect_consistent: 実行後の変化が事前予測と一致した場合True。未実行または未評価ならNone
#     requires_verification: SUSPICIOUS判定を解除するための確認が実行前に必要な場合True
#     verification_requests: 再観測や目標照合など、入力送信の前に済ませる確認項目
#     upstream_anomaly: 目的への貢献見込みは高い一方で、操作の危険度も高いという不一致がある場合True
#     evidence: 座標、操作種別、目的など、各検査が何を根拠に判定したかを示す一覧
#     decision_reliability: 選択候補が画面と許可一覧に合うかを調べた別検査の結果。操作の危険度とは混同しない
#   ]
# }
class ActionSafetyResult:
    assessment_id: str
    action_id: str
    status: SafetyStatus
    recognition_confidence: float
    safety_score: float
    risk_score: float
    risk_level: str
    reversible: bool | None
    blast_radius: str
    target_scope: str
    goal_alignment: bool | None
    expected_effect_consistent: bool | None
    requires_verification: bool
    verification_requests: tuple[str, ...]
    upstream_anomaly: bool
    evidence: tuple[SafetyEvidence, ...]
    decision_reliability: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.assessment_id.strip() or not self.action_id.strip():
            raise ValueError("safety result identity must not be empty")
        for name, value in (
            ("recognition_confidence", self.recognition_confidence),
            ("safety_score", self.safety_score),
            ("risk_score", self.risk_score),
        ):
            if not isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.risk_level not in {"low", "medium", "high", "critical"}:
            raise ValueError("invalid risk level")
        if self.blast_radius not in {"low", "medium", "high", "critical"}:
            raise ValueError("invalid blast radius")
        if self.target_scope not in {"local", "game", "session", "system"}:
            raise ValueError("invalid target scope")
        if self.status == SafetyStatus.SAFE and self.requires_verification:
            raise ValueError("SAFE result cannot require verification")

    # {
    #   責務: [
    #     to_dict: 安全評価と候補選択の信頼性を混ぜずに、ログへ保存できる辞書へ変換する
    #   ]
    #   処理: [
    #     1: Enumとタプルを文字列・一覧へ変換し、評価の各項目をスキーマ付き辞書へ格納する
    #     2: 各検査根拠も同じ辞書に含め、後から判定を追跡できる形にする
    #   ]
    #   引数: []
    #   戻り値: [
    #     dict[str, Any]: action-safety/v1形式の記録。JSONへ保存して評価理由を再確認できる
    #   ]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "action-safety/v1",
            "assessment_id": self.assessment_id,
            "action_id": self.action_id,
            "status": self.status.value,
            "recognition_confidence": self.recognition_confidence,
            "safety_score": self.safety_score,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "reversible": self.reversible,
            "blast_radius": self.blast_radius,
            "target_scope": self.target_scope,
            "goal_alignment": self.goal_alignment,
            "expected_effect_consistent": self.expected_effect_consistent,
            "requires_verification": self.requires_verification,
            "verification_requests": list(self.verification_requests),
            "upstream_anomaly": self.upstream_anomaly,
            "evidence": [entry.to_dict() for entry in self.evidence],
            "decision_reliability": self.decision_reliability,
        }


# {
#   責務: [
#     ActionSafetyEvaluator: 選択された操作が画面範囲・ゲーム目的・既知の危険条件に反しないか実行前に判定する
#   ]
#   フィールド: [
#     suspicious_threshold: risk_scoreがこの値以上なら注意対象としてSUSPICIOUS判定にする境界
#     high_risk_threshold: risk_scoreがこの値以上なら高リスクとして追加確認を求める境界
#   ]
#   処理: [
#     1: 操作種別、画面内座標、対象範囲、目的との整合、予測した効果を規則で検査する
#     2: 実行禁止・追加確認・既知の危険なしのいずれかと、その根拠を返す
#     3: 操作の危険度と候補選択の信頼性を別の記録として保つ
#   ]
#   補足: [
#     ここでは危険度を評価する。実際の入力送信を最後に遮断する安全装置の代わりにはならない
#   ]
# }
class ActionSafetyEvaluator:

    SUPPORTED_KINDS = frozenset({"click", "double_click", "key", "wait"})
    IRREVERSIBLE_TERMS = (
        "delete save",
        "erase save",
        "wipe save",
        "overwrite save",
        "reset save",
        "delete data",
        "erase data",
        "new game overwrite",
    )
    SESSION_TERMS = (
        "quit game",
        "exit game",
        "close game",
        "restart game",
        "reset game",
        "return to desktop",
    )
    TRANSACTION_TERMS = (
        "confirm purchase",
        "real money",
        "spend currency",
        "buy with",
    )
    SYSTEM_KEY_TERMS = (
        "alt+f4",
        "alt f4",
        "ctrl+alt+delete",
        "ctrl alt delete",
        "windows key",
        "win+",
    )

    # {
    #   責務: [__init__: 注意判定と高リスク判定を切り替える点数の境界を設定する]
    #   引数: [suspicious_threshold: SUSPICIOUS判定を始める危険点数, high_risk_threshold: 高リスクとして追加確認を強める点数]
    #   戻り値: []
    #   エラー: [ValueError: 0から1の範囲外、または注意境界が高リスク境界を超える場合]
    # }
    def __init__(self, suspicious_threshold: float = 0.45, high_risk_threshold: float = 0.65) -> None:
        if not 0.0 <= suspicious_threshold <= high_risk_threshold <= 1.0:
            raise ValueError("safety thresholds must satisfy 0 <= suspicious <= high-risk <= 1")
        self.suspicious_threshold = suspicious_threshold
        self.high_risk_threshold = high_risk_threshold

    # {
    #   責務: [
    #     evaluate: 画面上で選ばれた操作候補を検査し、安全判定と追加確認事項を返す
    #   ]
    #   処理: [
    #     1: クリック位置や範囲が現在の画面内か、操作種別や説明が危険語に当たるか確認する
    #     2: ゲーム目標と予測効果に照らして追加確認の要否を決める
    #     3: 危険度の根拠と、上流で行った候補選択検査を別項目として結果に残す
    #   ]
    #   引数: [
    #     observation: 操作位置が画面内か検証するための現在画面と幅・高さ
    #     candidate: 種類、対象座標、説明、認識確信度を持つ今回の操作候補
    #     context: 現在のゲーム目標、予測効果、候補選択の検査記録。省略時は未評価として扱う
    #   ]
    #   戻り値: [
    #     ActionSafetyResult: SAFE・SUSPICIOUS・BLOCKの判定、点数、根拠、必要な追加確認
    #   ]
    # }
    def evaluate(
        self,
        observation: ScreenObservation,
        candidate: ActionCandidate,
        context: SafetyEvaluationContext | None = None,
    ) -> ActionSafetyResult:
        context = context or SafetyEvaluationContext()
        evidence: list[SafetyEvidence] = []
        verification: list[str] = []
        risk_score = 0.05
        reversible: bool | None = context.reversible_hint
        blast_radius = "low"
        target_scope = context.target_scope_hint or "local"
        goal_alignment: bool | None = None

        # {
        #   責務: [add: 1つの安全規則の結論を、評価結果へ表示する根拠として追加する]
        #   引数: [check: 検査項目名, severity: info・warning・blockの扱い, confidence: 根拠の確信度, message: 検査対象と結果の説明]
        #   戻り値: [なし。evidence一覧へ根拠を1件追加する]
        # }
        def add(check: str, severity: str, confidence: float, message: str) -> None:
            evidence.append(SafetyEvidence(check, severity, confidence, message))

        if candidate.kind not in self.SUPPORTED_KINDS:
            add("action_schema", "block", 1.0, f"unsupported action kind: {candidate.kind}")
            risk_score = 1.0

        if candidate.kind in {"click", "double_click"}:
            if candidate.x is None or candidate.y is None:
                add("spatial_invariant", "block", 1.0, "click action has no target coordinates")
                risk_score = 1.0
            elif not (0 <= candidate.x < observation.width and 0 <= candidate.y < observation.height):
                add("spatial_invariant", "block", 1.0, "target coordinates are outside the current screen")
                risk_score = 1.0
            if candidate.bbox is not None:
                left, top, width, height = candidate.bbox
                if left < 0 or top < 0 or left + width > observation.width or top + height > observation.height:
                    add("spatial_invariant", "block", 1.0, "candidate bounding box crosses the current screen boundary")
                    risk_score = 1.0
                elif candidate.x is not None and candidate.y is not None and not (
                    left <= candidate.x < left + width and top <= candidate.y < top + height
                ):
                    add("target_consistency", "warning", 0.95, "target coordinate is outside its recognized bounding box")
                    verification.append("reobserve_target")
                    risk_score = max(risk_score, 0.5)

        if candidate.dangerous:
            add("explicit_danger", "block", 1.0, "candidate is explicitly marked dangerous")
            risk_score = 1.0
            reversible = False
            blast_radius = "critical"
            target_scope = "game"

        normalized = self._normalize(f"{candidate.action_id} {candidate.label}")
        intent: str | None = None
        if self._contains(normalized, self.IRREVERSIBLE_TERMS):
            intent = "irreversible_data_change"
            risk_score = max(risk_score, 0.9)
            reversible = False
            blast_radius = "high"
            target_scope = "game"
            add("semantic_risk", "warning", 0.95, "action appears to modify or erase persistent game data")
            verification.extend(("confirm_irreversible_action", "independent_safety_evidence"))
        elif self._contains(normalized, self.SESSION_TERMS):
            intent = "session_control"
            risk_score = max(risk_score, 0.72)
            reversible = False if "quit" in normalized or "close" in normalized else reversible
            blast_radius = "high"
            target_scope = "session"
            add("semantic_risk", "warning", 0.9, "action appears to terminate or reset the current game session")
            verification.append("independent_safety_evidence")
        elif self._contains(normalized, self.TRANSACTION_TERMS):
            intent = "transaction"
            risk_score = max(risk_score, 0.7)
            blast_radius = "medium"
            target_scope = "game"
            add("semantic_risk", "warning", 0.85, "action appears to commit a purchase or resource-spending operation")
            verification.append("independent_safety_evidence")

        if candidate.kind == "key" and self._contains(normalized, self.SYSTEM_KEY_TERMS):
            intent = "system_control"
            risk_score = max(risk_score, 0.9)
            blast_radius = "critical"
            target_scope = "system"
            add("target_scope", "warning", 0.95, "key action may escape the game target or affect the operating system")
            verification.extend(("verify_target_scope", "independent_safety_evidence"))

        goal = self._normalize(" ".join(part for part in (context.current_goal, context.short_term_goal) if part))
        if intent is not None:
            if goal:
                goal_alignment = self._goal_supports(intent, goal)
                if not goal_alignment:
                    add("goal_alignment", "warning", 0.9, "high-risk action is not supported by the current goal")
                    verification.append("verify_goal_alignment")
                    risk_score = max(risk_score, 0.78)
            else:
                goal_alignment = None
                add("goal_alignment", "warning", 0.7, "high-risk action has no goal evidence")
                verification.append("verify_goal_alignment")

        expected_consistency = context.expected_effect_consistent
        if expected_consistency is False:
            add("expected_effect", "warning", 0.9, "candidate conflicts with the expected transition or effect")
            verification.append("verify_expected_effect")
            risk_score = max(risk_score, 0.75)

        if candidate.confidence < 0.5:
            add(
                "recognition_confidence",
                "info",
                1.0,
                "recognition confidence is low; this is tracked separately from safety",
            )
            if risk_score >= self.high_risk_threshold:
                add("high_risk_low_recognition", "warning", 0.9, "high-risk target also has weak recognition evidence")
                verification.append("reobserve_target")

        upstream_anomaly = bool(
            context.utility_score is not None
            and context.utility_score >= 0.75
            and risk_score >= self.high_risk_threshold
        )
        if upstream_anomaly:
            add("upstream_score_anomaly", "warning", context.utility_confidence or 0.7, "high utility conflicts with high semantic risk")
            verification.append("recheck_upstream_evaluation")

        blocked = any(entry.severity == "block" for entry in evidence)
        warned = any(entry.severity == "warning" for entry in evidence)
        if blocked:
            status = SafetyStatus.BLOCK
        elif warned or risk_score >= self.suspicious_threshold:
            status = SafetyStatus.SUSPICIOUS
        else:
            status = SafetyStatus.SAFE

        verification_requests = tuple(dict.fromkeys(verification))
        requires_verification = status == SafetyStatus.SUSPICIOUS and bool(verification_requests)
        warning_count = sum(entry.severity == "warning" for entry in evidence)
        safety_score = 0.0 if status == SafetyStatus.BLOCK else max(0.0, min(1.0, 1.0 - risk_score - 0.08 * warning_count))
        return ActionSafetyResult(
            uuid4().hex,
            candidate.action_id,
            status,
            candidate.confidence,
            round(safety_score, 6),
            round(risk_score, 6),
            self._risk_level(risk_score),
            reversible,
            blast_radius,
            target_scope,
            goal_alignment,
            expected_consistency,
            requires_verification,
            verification_requests,
            upstream_anomaly,
            tuple(evidence),
            context.decision_reliability,
        )

    # {
    #   責務: [_normalize: 候補名と目的文の英字大小・区切りを揃え、危険語を照合できる形にする]
    #   引数: [text: 照合対象の操作名またはゲーム目標]
    #   戻り値: [str: 小文字化し、ハイフン・下線・連続空白を半角空白へ揃えた文字列]
    # }
    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.casefold().replace("_", " ").replace("-", " ").split())

    # {
    #   責務: [_contains: 正規化済みの操作説明に、指定した危険意図の語句が含まれるか調べる]
    #   引数: [text: 正規化済みの操作名と表示名, terms: 検索する語句の一覧]
    #   戻り値: [bool: いずれかの語句が含まれる場合True]
    # }
    @staticmethod
    def _contains(text: str, terms: tuple[str, ...]) -> bool:
        return any(term in text for term in terms)

    # {
    #   責務: [_risk_level: 危険点数を4段階の表示区分へ変換する]
    #   引数: [score: 0から1の範囲に収めた危険点数]
    #   戻り値: [str: low・medium・high・criticalの区分名]
    # }
    @staticmethod
    def _risk_level(score: float) -> str:
        if score < 0.25:
            return "low"
        if score < 0.5:
            return "medium"
        if score < 0.8:
            return "high"
        return "critical"

    # {
    #   責務: [_goal_supports: ゲーム目標が危険操作の実行を明示的に求めているか照合する]
    #   引数: [intent: 操作の危険な目的を示す分類名, goal: 小文字と空白を揃えたゲーム目標]
    #   戻り値: [bool: 分類に対応する必要語句が目標に含まれる場合True]
    # }
    @staticmethod
    def _goal_supports(intent: str, goal: str) -> bool:
        terms = {
            "irreversible_data_change": ("delete save", "erase save", "reset save", "start over", "new game"),
            "session_control": ("quit game", "exit game", "close game", "restart game", "reset game", "stop playing"),
            "transaction": ("purchase", "buy", "spend", "shop"),
            "system_control": ("close game", "switch window", "operating system", "desktop"),
        }
        return any(term in goal for term in terms.get(intent, ()))


# {
#   責務: [ActionSafetyAuditLog: 安全判定の識別IDごとに、判定・実際に送った入力・後から確認したゲーム結果を追記保存する]
#   記録内容: [evaluationは実行前の危険判定, executionは入力を送った結果, actual_outcomeは実行後のゲーム状態]
#   保存先: [path: 旧JSON配列を読む保存先, event_adapter: 現在のSession Journalへ追記する変換器]
# }
class ActionSafetyAuditLog:
    """安全判定の識別IDごとに、判定・入力・実行後の結果を追記保存する。"""

    # {
    #   責務: [__init__: 安全評価履歴を読む旧JSON保存先と、現在の記録先へ接続する変換器を設定する]
    #   処理: [変換器がある場合は追記先として使い、ない場合はpathのJSON配列を読み書きする]
    #   引数: [path: action_safety.jsonの保存先, event_adapter: 実行セッションの追記型履歴へ既存記録を移行して追記する変換器]
    #   戻り値: []
    # }
    def __init__(self, path: Path, event_adapter: LegacyEventAdapter | None = None) -> None:
        self.path = path
        self.event_adapter = event_adapter

    # {
    #   責務: [append_evaluation: 操作を送る前に行った危険度判定を、その判定に使った画面とともに保存する]
    #   処理: [評価結果・目標・画面観測IDを1件の記録へまとめる]
    #   引数: [result: ActionSafetyEvaluatorが返した危険度判定, snapshot_id: 判断に使った画面観測ID, goal: 判断元へ渡した目標]
    #   戻り値: []
    # }
    def append_evaluation(self, result: ActionSafetyResult, *, snapshot_id: str = "", goal: str = "") -> None:
        self._append(
            {
                "event": "evaluation",
                "assessment_id": result.assessment_id,
                "action_id": result.action_id,
                "snapshot_id": snapshot_id,
                "goal": goal,
                "result": result.to_dict(),
            }
        )

    # {
    #   責務: [append_execution: 実行前の危険度判定に対応する入力送信結果を保存する]
    #   処理: [操作ID・実行可否・入力方式・結果説明を1件の記録へまとめる]
    #   引数: [assessment_id: 入力送信前に行った危険度判定のID, execution: ActionExecutorが返した入力結果]
    #   戻り値: []
    #   エラー: [ValueError: assessment_idが空の場合]
    # }
    def append_execution(self, assessment_id: str, execution: Any) -> None:
        if not assessment_id.strip():
            raise ValueError("assessment_id must not be empty")
        self._append(
            {
                "event": "execution",
                "assessment_id": assessment_id,
                "action_id": str(getattr(execution, "action_id", "")),
                "executed": bool(getattr(execution, "executed", False)),
                "mode": str(getattr(execution, "mode", "")),
                "detail": str(getattr(execution, "detail", "")),
            }
        )

    # {
    #   責務: [append_outcome: 入力後に観測したゲーム結果を、入力前の危険度判定へ関連付ける]
    #   処理: [結果区分・確信度・観測根拠を検証し、実行後の結果として追記する]
    #   引数: [assessment_id: 結果の対象となる危険度判定ID, status: successは成功、failureは失敗、ongoingは進行中、unknownは不明, confidence: 結果判定の確信度, evidence: statusを支持する観測内容]
    #   戻り値: []
    #   エラー: [ValueError: ID・status・confidenceが契約を満たさない場合]
    # }
    def append_outcome(self, assessment_id: str, status: str, confidence: float, evidence: str = "") -> None:
        if not assessment_id.strip():
            raise ValueError("assessment_id must not be empty")
        if status not in {"success", "failure", "ongoing", "unknown"}:
            raise ValueError("invalid outcome status")
        if not isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("outcome confidence must be between 0 and 1")
        self._append(
            {
                "event": "actual_outcome",
                "assessment_id": assessment_id,
                "status": status,
                "confidence": confidence,
                "evidence": evidence,
            }
        )

    # {
    #   責務: [entries: 危険度判定・入力送信・実行後結果の追記履歴を、旧JSON利用者と同じ形で返す]
    #   処理: [Journal変換器の記録、または保存済みJSON配列から辞書形式の記録を取得する]
    #   引数: []
    #   戻り値: [list[dict[str, Any]]: evaluationは事前判定、executionは入力結果、actual_outcomeはゲーム結果を示す記録一覧]
    # }
    def entries(self) -> list[dict[str, Any]]:
        return self._read()

    # {
    #   責務: [_append: 新しい危険度判定・入力結果・ゲーム結果を追記履歴へ追加する]
    #   処理: [Journal変換器があればそこへ送り、なければ一時JSONから保存先を置き換える]
    #   引数: [event: 種別と関連IDを含む、旧JSON利用者にも読める記録]
    #   戻り値: []
    # }
    def _append(self, event: dict[str, Any]) -> None:
        if self.event_adapter is not None:
            self.event_adapter.prepare_append(event).publish()
            return
        entries = self._read()
        entries.append(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    # {
    #   責務: [_read: 保存済みの危険度判定・入力結果・ゲーム結果を変更せず読み込む]
    #   処理: [Journal変換器の互換記録またはJSON配列を返し、配列以外の形式を拒否する]
    #   引数: [self: Safety監査store]
    #   戻り値: [list[dict[str, Any]]: 保存順に並んだ評価・入力・結果の記録]
    #   エラー: [OSError: 保存ファイルを読めない場合, ValueError: JSONの最上位が配列ではない場合]
    # }
    def _read(self) -> list[dict[str, Any]]:
        if self.event_adapter is not None:
            return self.event_adapter.records
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("action safety audit log must contain an array")
        return [entry for entry in value if isinstance(entry, dict)]
