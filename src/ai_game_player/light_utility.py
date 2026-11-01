"""#409の完全なUtility推定器とは区別した、有界の数値EWMA接続口。"""

from dataclasses import dataclass
from math import isfinite

from ai_game_player.knowledge_scope import validate_identifier


# {
#   責務: [UtilityMeasurement: 推論経路から数値だけを取り込むversioned sample]
#   フィールド: [method_id: 手法ID, latency_ms: 実測時間, quality: 任意の検証済み成果値, schema_version: 契約版]
# }
@dataclass(frozen=True, slots=True)
class UtilityMeasurement:
    method_id: str
    latency_ms: float
    quality: float | None = None
    schema_version: int = 1

    # {
    #   責務: [__post_init__: 未測定成果をゼロや成功として補完しない]
    #   処理: [ID・版・有限数値を検証する] 引数: [self: sample] 戻り値: [なし]
    #   エラー: [ValueError: 不正な測定値]
    # }
    def __post_init__(self) -> None:
        validate_identifier(self.method_id, "utility method")
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.latency_ms) not in (int, float)
            or not isfinite(self.latency_ms)
            or self.latency_ms < 0
            or (
                self.quality is not None
                and (
                    type(self.quality) not in (int, float)
                    or not isfinite(self.quality)
                    or not -1 <= self.quality <= 1
                )
            )
        ):
            raise ValueError("invalid numeric utility measurement")


# {
#   責務: [UtilityEstimate: workerが生成する変更不能の手法別EWMA]
#   フィールド: [method_id/count: 系列と件数, latency_ms: 時間EWMA,
#     quality/quality_count: verified成果がある場合だけの成果EWMAと件数]
# }
@dataclass(frozen=True, slots=True)
class UtilityEstimate:
    method_id: str
    count: int
    latency_ms: float
    quality: float | None
    quality_count: int


# {
#   責務: [update_utility: boundedな手法表をimmutableな次版へ更新する]
#   処理: [固定alpha=.1のEWMA、未知成果は成果件数を増やさず、最大64手法で新系列を拒否する]
#   引数: [previous: 既存の不変表, sample: 一件の数値実測] 戻り値: [tuple: 新しい不変表]
# }
def update_utility(
    previous: tuple[UtilityEstimate, ...], sample: UtilityMeasurement
) -> tuple[UtilityEstimate, ...]:
    items = {item.method_id: item for item in previous}
    old = items.get(sample.method_id)
    if old is None:
        if len(items) >= 64:
            return previous
        new = UtilityEstimate(
            sample.method_id,
            1,
            sample.latency_ms,
            sample.quality,
            int(sample.quality is not None),
        )
    else:
        quality = old.quality
        if sample.quality is not None:
            quality = (
                sample.quality
                if quality is None
                else quality + 0.1 * (sample.quality - quality)
            )
        new = UtilityEstimate(
            sample.method_id,
            old.count + 1,
            old.latency_ms + 0.1 * (sample.latency_ms - old.latency_ms),
            quality,
            old.quality_count + int(sample.quality is not None),
        )
    items[sample.method_id] = new
    return tuple(items[key] for key in sorted(items))
