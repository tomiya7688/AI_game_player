"""評価器と汎用plannerが共有する、標準Pythonだけに依存した数値品質契約。"""

from dataclasses import dataclass
from math import isfinite
import re
from typing import cast


QUALITY_PROJECTION_VERSION = 1
_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


# {
#   責務: [NumericQuality: 観測/候補の意味を外側に保ったまま有限の品質と不確かさを渡す]
#   フィールド: [value: -1..1の条件付き品質, confidence: 0..1の証拠支持率。校正済み成功確率ではない,
#     available: 数値根拠の有無, projection_version/profile_version: 契約と適用方針の版,
#     provenance: raw説明を含まない最大8つのopaque参照, coverage: 全weight中の有効軸比率,
#     missing_axes: 最大16件の欠損参照。actual/hypotheticalの判定は外側で行う]
# }
@dataclass(frozen=True, slots=True)
class NumericQuality:
    value: float = 0.0
    confidence: float = 0.0
    available: bool = False
    projection_version: int = QUALITY_PROJECTION_VERSION
    profile_version: str = "default.1"
    provenance: tuple[str, ...] = ()
    coverage: float = 1.0
    missing_axes: tuple[str, ...] = ()

    # {
    #   責務: [__post_init__: 未知を確信ある0点として渡さず、不正数値や自由文参照を拒否する]
    #   処理: [有限範囲・版・tuple容量を検証し、unavailableはvalue/confidenceとも0に限定する]
    #   引数: [self: 構築する不変品質] 戻り値: [なし] エラー: [ValueError: 契約違反]
    # }
    def __post_init__(self) -> None:
        if (
            type(self.value) not in (int, float)
            or not -1 <= self.value <= 1
            or not isfinite(self.value)
            or type(self.confidence) not in (int, float)
            or not 0 <= self.confidence <= 1
            or not isfinite(self.confidence)
            or type(self.available) is not bool
            or type(self.projection_version) is not int
            or self.projection_version != QUALITY_PROJECTION_VERSION
            or type(self.provenance) is not tuple
            or len(self.provenance) > 8
            or type(self.coverage) not in (int, float)
            or not 0 <= self.coverage <= 1
            or not isfinite(self.coverage)
            or type(self.missing_axes) is not tuple
            or len(self.missing_axes) > 16
        ):
            raise ValueError("invalid numeric evaluation quality")
        for reference in (self.profile_version, *self.provenance, *self.missing_axes):
            if (
                not isinstance(reference, str)
                or _REFERENCE.fullmatch(reference) is None
                or reference.startswith(("sk-", "ghp_", "github_pat_"))
            ):
                raise ValueError(
                    "evaluation references must be bounded opaque identifiers"
                )
        if not self.available and (self.value != 0 or self.confidence != 0):
            raise ValueError("unavailable quality must preserve zero support")
        if not self.available:
            object.__setattr__(self, "coverage", 0.0)
        if self.confidence > self.coverage + 1e-12:
            raise ValueError("quality confidence must include missing coverage")

    # {
    #   責務: [effective_value: 支持率で縮小したsoft順位付け値を返す]
    #   処理: [未知は0、利用可能な品質はvalue×confidenceとする。Safety条件は作らない]
    #   引数: [なし] 戻り値: [float: -1..1の保守的なsoft寄与]
    # }
    @property
    def effective_value(self) -> float:
        return self.value * self.confidence if self.available else 0.0

    # {
    #   責務: [bounds: 支持しない質量を未知[-1,1]に残す有限の品質envelopeを返す]
    #   処理: [effective_valueの周囲へ1-confidenceを加え範囲制限する。統計CIやchance確率ではない]
    #   引数: [なし] 戻り値: [tuple: 保守的な下限と上限。unknownは[-1,1]]
    # }
    @property
    def bounds(self) -> tuple[float, float]:
        uncertainty = 1.0 - self.confidence
        return max(-1.0, self.effective_value - uncertainty), min(
            1.0, self.effective_value + uncertainty
        )

    # {
    #   責務: [to_dict: 品質・支持率・版・出所を独立したJSON値へ変換する]
    #   処理: [provenance tupleを複写し保持値を可変にしない]
    #   引数: [なし] 戻り値: [dict: 数値契約の独立表現]
    # }
    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "confidence": self.confidence,
            "available": self.available,
            "projection_version": self.projection_version,
            "profile_version": self.profile_version,
            "provenance": list(self.provenance),
            "coverage": self.coverage,
            "missing_axes": list(self.missing_axes),
        }

    # {
    #   責務: [from_dict: JSONの品質を既知フィールドだけで再検証する]
    #   処理: [余分/欠落、provenance配列以外を拒否しconstructorの数値境界へ渡す]
    #   引数: [value: 数値契約のJSON表現] 戻り値: [NumericQuality: 検証済み不変品質]
    #   エラー: [ValueError: field/type/値不正]
    # }
    @classmethod
    def from_dict(cls, value: object) -> "NumericQuality":
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "value",
                "confidence",
                "available",
                "projection_version",
                "profile_version",
                "provenance",
                "coverage",
                "missing_axes",
            }
            or not isinstance(value["provenance"], list)
            or len(value["provenance"]) > 8
            or not isinstance(value["missing_axes"], list)
            or len(value["missing_axes"]) > 16
        ):
            raise ValueError("numeric quality fields do not match the contract")
        return cls(
            cast(float, value["value"]),
            cast(float, value["confidence"]),
            cast(bool, value["available"]),
            cast(int, value["projection_version"]),
            cast(str, value["profile_version"]),
            tuple(value["provenance"]),
            cast(float, value["coverage"]),
            tuple(value["missing_axes"]),
        )
