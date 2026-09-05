from dataclasses import dataclass, field
from math import isfinite
from typing import Any


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
class ActionCandidate:
    action_id: str
    kind: str
    label: str
    x: int | None = None
    y: int | None = None
    confidence: float = 1.0
    dangerous: bool = False

    def __post_init__(self) -> None:
        if not self.action_id.strip():
            raise ValueError("action_id must not be empty")
        if not self.kind.strip():
            raise ValueError("kind must not be empty")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be provided together")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ActionCandidate":
        if not isinstance(value, dict):
            raise ValueError("action candidate must be an object")
        x = value.get("x")
        y = value.get("y")
        return cls(str(value["action_id"]), str(value["kind"]), str(value.get("label", value["action_id"])), int(x) if x is not None else None, int(y) if y is not None else None, float(value.get("confidence", 1.0)), bool(value.get("dangerous", False)))

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class ActionDecision:
    action_id: str
    reason: str
    provider: str

    def to_dict(self) -> dict[str, str]:
        return self.__dict__.copy()