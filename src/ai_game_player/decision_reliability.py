from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from typing import Any

from ai_game_player.models import ActionDecision


# {
#   責務: [
#     ReliabilityStatus: 候補選択の検査結果を、後続処理が実行・確認・拒否のどれに扱うかを4段階で示す
#   ]
#   フィールド: [
#     TRUST: この検査で見つかった問題がなく、候補を次の処理へ渡せる。正しさを保証する状態ではない
#     CAUTION: 選択理由などに情報不足があるため、候補は保持するが通常より慎重に扱う
#     VERIFY: 画面や状態の照合に必要な情報が足りず、追加の観測・確認を済ませるまで実入力へ進めない
#     REJECT: 形式、許可候補、画面状態に明確な不整合があり、選択を無効として実行しない
#   ]
# }
class ReliabilityStatus(str, Enum):
    TRUST = "TRUST"
    CAUTION = "CAUTION"
    VERIFY = "VERIFY"
    REJECT = "REJECT"


@dataclass(frozen=True)
# {
#   責務: [
#     ReliabilityEvidence: 候補選択の各検査で比較した情報と結論を、後から理由を追える単位で保持する
#   ]
#   フィールド: [
#     check: どの不整合を調べたか識別する検査名。集計時の分類にも使う
#     severity: この結果が最終状態へ与える扱い。infoは記録のみ、cautionは注意、verifyは再確認、rejectは無効
#     confidence: 検査結果をどれだけ確かなものとみなすかを示す0から1の値
#     message: 人が読んで、何を比較し何が分かったか理解できる説明
#     source: 判定を生成した規則または検査器の識別名
#     details: 期待値と受け取った値など、messageの結論を検証するための比較情報
#   ]
# }
class ReliabilityEvidence:
    check: str
    severity: str
    confidence: float
    message: str
    source: str = "decision_verifier/v1"
    details: dict[str, Any] = field(default_factory=dict)

    # {
    #   責務: [
    #     __post_init__: 検査記録に空の識別情報や定義外の深刻度・確信度が入らないよう生成時に検証する
    #   ]
    #   処理: [
    #     1: 検査名、説明、出所に空白以外の文字があることを確かめる
    #     2: 深刻度が定義済みで、確信度が有限な0から1の値であることを確かめる
    #   ]
    #   引数: []
    #   戻り値: [なし。検証に失敗した場合はValueErrorを送出する]
    #   エラー: [
    #     ValueError: 必須文字列が空、深刻度が未定義、または確信度が0から1の範囲外の場合
    #   ]
    # }
    def __post_init__(self) -> None:
        if not self.check.strip() or not self.message.strip() or not self.source.strip():
            raise ValueError("reliability evidence metadata must not be empty")
        if self.severity not in {"info", "caution", "verify", "reject"}:
            raise ValueError("invalid reliability evidence severity")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("reliability evidence confidence must be between 0 and 1")

    # {
    #   責務: [
    #     to_dict: 検査記録をJSONへ保存し、後から同じ比較内容を読める辞書へ変換する
    #   ]
    #   処理: [
    #     1: 検査名・深刻度・確信度・説明・出所・比較情報を、キー名を保って辞書へ格納する
    #   ]
    #   引数: []
    #   戻り値: [
    #     dict[str, Any]: JSON化可能な検査記録。detailsがない場合は空の辞書として含める
    #   ]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "confidence": self.confidence,
            "message": self.message,
            "source": self.source,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
# {
#   責務: [
#     ReliabilityResult: 1回の候補選択を検査した総合状態と、その状態に至った全根拠を結び付けて保持する
#   ]
#   フィールド: [
#     assessment_id: 1回の検査結果をログや安全評価から参照するための一意なID
#     status: 検査結果の扱い。TRUSTは通過、CAUTIONは注意、VERIFYは追加確認、REJECTは無効
#     snapshot_id: 候補選択時に使った画面観測を特定し、別時点の判断との取り違えを防ぐID
#     action_id: 判断元が選んだ候補のID。REJECT時は回答を採用しないため空になることがある
#     provider: この判断を返したモデルまたは判断処理の識別名。原因の追跡に使う
#     evidence: 総合状態の根拠となった検査結果を、検査単位で並べた一覧
#   ]
# }
class ReliabilityResult:
    assessment_id: str
    status: ReliabilityStatus
    snapshot_id: str
    action_id: str
    provider: str
    evidence: tuple[ReliabilityEvidence, ...]

    # {
    #   責務: [
    #     to_dict: 検査結果を版識別子付きの辞書へ変換し、保存後も状態と根拠の対応を保つ
    #   ]
    #   処理: [
    #     1: 検査ID、状態、画面ID、選択候補、判断元を所定のキーで格納する
    #     2: 各根拠を個別の辞書へ変換し、一覧として結果に含める
    #   ]
    #   引数: []
    #   戻り値: [
    #     dict[str, Any]: reliability/v1形式の記録。ログ保存と検査理由の再確認に使う
    #   ]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "reliability/v1",
            "assessment_id": self.assessment_id,
            "status": self.status.value,
            "snapshot_id": self.snapshot_id,
            "action_id": self.action_id,
            "provider": self.provider,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
# {
#   責務: [
#     _DecisionFields: 判断元の出力から型と必須値を確認できた情報だけを、後続検査用にまとめる
#   ]
#   フィールド: [
#     action_id: 判断元が出力した操作ID。許可候補一覧との一致は後続の検査で確かめる
#     reason: その候補を選んだ理由。選択候補との明示的な矛盾を検査する
#     provider: 判断を返したモデルまたは処理の識別名。監査記録に残す
#     snapshot_id: 判断元へ提示した画面観測のID。回答が別の画面に基づかないか照合する
#     screen_id: 回答が参照した画面のID。検査時点で観測した画面と一致するか照合する
#     state_signature: 回答が参照した画面状態の照合文字列。古い状態への判断でないか調べる
#   ]
# }
class _DecisionFields:
    action_id: str
    reason: str
    provider: str
    snapshot_id: str | None
    screen_id: str | None
    state_signature: str | None


# {
#   責務: [
#     _decision_fields: 判断元から届いた未検証の値が決められた形式と型を満たす場合だけ、検査用の値へ変換する
#   ]
#   処理: [
#     1: ActionDecisionまたは文字列キーを持つ辞書形式の値から判断項目を取り出す
#     2: 未知のキー、欠落した必須値、誤った型、空の識別値を拒否する
#     3: 全項目が有効な場合だけ、後続検査用の値を返す
#   ]
#   引数: [
#     decision_output: 判断元が返した未検証の値。正しい型や項目名だとは仮定しない
#   ]
#   戻り値: [
#     _DecisionFields | None: 形式が有効なら正規化した判断項目、不正ならNone
#   ]
# }
def _decision_fields(decision_output: object) -> _DecisionFields | None:
    if isinstance(decision_output, ActionDecision):
        if decision_output.validation_error:
            return None
        record: Mapping[str, Any] = decision_output.to_dict()
    elif isinstance(decision_output, Mapping):
        record = decision_output
    else:
        return None

    allowed_fields = {"action_id", "reason", "provider", "snapshot_id", "screen_id", "state_signature"}
    if set(record).difference(allowed_fields):
        return None
    required_fields = ("action_id", "reason", "provider")
    if any(name not in record for name in required_fields):
        return None
    action_id, reason, provider = (record[name] for name in required_fields)
    if not all(isinstance(value, str) for value in (action_id, reason, provider)):
        return None
    if not action_id.strip() or not provider.strip():
        return None

    optional_fields: list[str | None] = []
    for field_name in ("snapshot_id", "screen_id", "state_signature"):
        field_value = record.get(field_name)
        if field_value is not None and not isinstance(field_value, str):
            return None
        optional_fields.append(field_value)
    return _DecisionFields(action_id, reason, provider, *optional_fields)
