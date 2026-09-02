from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class ScreenObservation:
    screen_id: str
    width: int
    height: int
    ocr_text: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]:
        return {"screen_id":self.screen_id,"width":self.width,"height":self.height,"ocr_text":self.ocr_text,"features":self.features}

@dataclass(frozen=True)
class ActionCandidate:
    action_id: str
    kind: str
    label: str
    x: int|None = None
    y: int|None = None
    confidence: float = 1.0
    dangerous: bool = False
    def to_dict(self) -> dict[str, Any]: return self.__dict__.copy()

@dataclass(frozen=True)
class ActionDecision:
    action_id: str
    reason: str
    provider: str
    def to_dict(self) -> dict[str, str]: return self.__dict__.copy()
