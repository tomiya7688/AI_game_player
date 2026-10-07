from dataclasses import dataclass, field
from math import isfinite
from typing import Any


def _parse_bbox(value: Any) -> tuple[int, int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("bbox must contain exactly four values")
    return (int(value[0]), int(value[1]), int(value[2]), int(value[3]))



@dataclass(frozen=True)
class ScreenObservation:
    screen_id: str
    width: int
    height: int
    ocr_text: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.screen_id).strip():
            raise ValueError("screen_id must not be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("screen dimensions must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {"screen_id": self.screen_id, "width": self.width, "height": self.height, "ocr_text": self.ocr_text, "features": self.features}


@dataclass(frozen=True)
class DetectedElement:
    element_id: str
    element_type: str
    bbox: tuple[int, int, int, int]
    source: str
    confidence: float = 1.0
    text: str | None = None
    kind: str = "click"
    dangerous: bool = False

    def __post_init__(self) -> None:
        if not self.element_id.strip():
            raise ValueError("element_id must not be empty")
        if not self.element_type.strip():
            raise ValueError("element_type must not be empty")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if len(self.bbox) != 4 or self.bbox[2] <= 0 or self.bbox[3] <= 0:
            raise ValueError("bbox must be (x, y, positive_width, positive_height)")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DetectedElement":
        if not isinstance(value, dict):
            raise ValueError("detected element must be an object")
        element_id = value.get("element_id", value.get("action_id"))
        if element_id is None:
            raise ValueError("detected element requires element_id")
        bbox = value.get("bbox")
        if bbox is None:
            raise ValueError("detected element requires bbox")
        text = value.get("text", value.get("label"))
        return cls(
            str(element_id),
            str(value.get("element_type", "region")),
            _parse_bbox(bbox),
            str(value.get("source", "unknown")),
            float(value.get("confidence", 1.0)),
            str(text) if text is not None else None,
            str(value.get("kind", "click")),
            bool(value.get("dangerous", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ActionCandidate:
    action_id: str
    kind: str
    label: str
    x: int | None = None
    y: int | None = None
    confidence: float = 1.0
    dangerous: bool = False
    bbox: tuple[int, int, int, int] | None = None
    hold_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.action_id.strip():
            raise ValueError("action_id must not be empty")
        if not self.kind.strip():
            raise ValueError("kind must not be empty")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be provided together")
        if self.bbox is not None:
            if len(self.bbox) != 4 or self.bbox[2] <= 0 or self.bbox[3] <= 0:
                raise ValueError("bbox must be (x, y, positive_width, positive_height)")
        if self.hold_seconds is not None and (not isfinite(self.hold_seconds) or self.hold_seconds < 0):
            raise ValueError("hold_seconds must be finite and non-negative")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ActionCandidate":
        if not isinstance(value, dict):
            raise ValueError("action candidate must be an object")
        x = value.get("x")
        y = value.get("y")
        bbox = value.get("bbox")
        hold_seconds = value.get("hold_seconds")
        return cls(
            str(value["action_id"]),
            str(value["kind"]),
            str(value.get("label", value["action_id"])),
            int(x) if x is not None else None,
            int(y) if y is not None else None,
            float(value.get("confidence", 1.0)),
            bool(value.get("dangerous", False)),
            _parse_bbox(bbox) if bbox is not None else None,
            float(hold_seconds) if hold_seconds is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
# {
#   責務: [
#     ActionDecision: 判断元が選んだ操作、その理由、回答が基づいた画面状態を実行前検証へ渡す
#   ]
#   フィールド: [
#     action_id: 判断元が選択した操作候補のID。後段は許可候補一覧と照合してから使う
#     reason: その操作を選んだ理由。選択候補を避ける内容でないか別途検査する
#     provider: 回答した判断元またはモデルの識別名。失敗や不整合の出所を調べるために残す
#     snapshot_id: 判断時に提示した画面観測のID。別時点の画面に対する回答でないか確かめる
#     screen_id: 回答が参照した画面のID。現在画面との一致を確認するために使う
#     state_signature: 回答が参照した画面内容の照合値。古い状態への回答を検知するために使う
#     validation_error: 判断元の応答形式を読み取れなかった理由。設定されている場合は判断を拒否する
#   ]
# }
class ActionDecision:
    action_id: str
    reason: str
    provider: str
    snapshot_id: str | None = None
    screen_id: str | None = None
    state_signature: str | None = None
    validation_error: str | None = None

    # {
    #   責務: [
    #     to_dict: 判断の必須情報と存在する場合だけ画面参照を、互換性を保った記録辞書へ変換する
    #   ]
    #   処理: [
    #     1: 操作ID、理由、判断元を常に含める
    #     2: Noneではない画面参照と検証エラーだけを追加し、従来の必須3項目の形式を崩さない
    #   ]
    #   引数: []
    #   戻り値: [
    #     dict[str, Any]: 必須判断情報と存在する任意参照を含む、JSON化可能な辞書
    #   ]
    # }
    def to_dict(self) -> dict[str, Any]:
        decision_record: dict[str, Any] = {
            "action_id": self.action_id,
            "reason": self.reason,
            "provider": self.provider,
        }
        # 参照を持たない過去の記録も同じ必須キーで読めるよう、未設定の参照キーは出力しない。
        for field_name in ("snapshot_id", "screen_id", "state_signature", "validation_error"):
            field_value = getattr(self, field_name)
            if field_value is not None:
                decision_record[field_name] = field_value
        return decision_record
