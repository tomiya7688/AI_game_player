"""軽量更新の有限予算と、資源分類器を置換しない数値admission契約。"""

from dataclasses import dataclass
from math import isfinite


# {
#   責務: [LightAdaptationBudget: queue・一回の仕事・永続書込の上限を宣言する]
#   フィールド: [queue_capacity/batch_size: 件数上限, max_event_bytes/max_memory_bytes: 保持量上限,
#     max_update_ms/max_cpu_percent: workerの時間上限, min_publish_seconds/max_writes_per_minute: IO頻度,
#     min_samples: 更新の最小batch, idle_flush_seconds: 小batchを回収する期限]
# }
@dataclass(frozen=True, slots=True)
class LightAdaptationBudget:
    queue_capacity: int = 128
    batch_size: int = 16
    max_event_bytes: int = 4096
    max_memory_bytes: int = 8 * 1024 * 1024
    max_update_ms: float = 20.0
    max_cpu_percent: float = 5.0
    min_publish_seconds: float = 5.0
    max_writes_per_minute: int = 12
    min_samples: int = 4
    idle_flush_seconds: float = 1.0

    # {
    #   責務: [__post_init__: 無限仕事や不整合な保持量を設定で作らせない]
    #   処理: [件数・byte・時間・割合の有限範囲を検証する]
    #   引数: [self: 不変予算] 戻り値: [なし] エラー: [ValueError: 契約外の予算]
    # }
    def __post_init__(self) -> None:
        for name, lower, upper in (
            ("queue_capacity", 1, 4096),
            ("batch_size", 4, 64),
            ("max_event_bytes", 512, 16384),
            ("max_memory_bytes", 65536, 64 * 1024 * 1024),
            ("max_writes_per_minute", 1, 120),
            ("min_samples", 4, 64),
        ):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("light adaptation integer budget outside range")
        for value in (
            self.max_update_ms,
            self.max_cpu_percent,
            self.min_publish_seconds,
            self.idle_flush_seconds,
        ):
            if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                raise ValueError(
                    "light adaptation time budget must be finite and positive"
                )
        if (
            self.max_cpu_percent > 100
            or self.max_update_ms > 1000
            or self.min_samples > self.batch_size
            or self.queue_capacity * self.max_event_bytes > self.max_memory_bytes // 2
        ):
            raise ValueError("light adaptation budgets are inconsistent")


# {
#   責務: [LightResourceSample: hostが観測した数値資源圧をworker admissionへ渡す]
#   フィールド: [cpu_percent: host CPU使用率, available_memory_bytes: 更新に利用可能なRAM,
#     deadline_remaining_ms: 次の操作deadlineまで, pressure: hostの過負荷通知, schema_version: 数値投影契約版]
# }
@dataclass(frozen=True, slots=True)
class LightResourceSample:
    cpu_percent: float = 0.0
    available_memory_bytes: int = 64 * 1024 * 1024
    deadline_remaining_ms: float = 1000.0
    pressure: bool = False
    schema_version: int = 1

    # {
    #   責務: [__post_init__: 未知や非有限の資源を安全な余力として扱わない]
    #   処理: [有限値と版を検証する] 引数: [self: 数値投影] 戻り値: [なし]
    #   エラー: [ValueError: 契約外の資源投影]
    # }
    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or type(self.pressure) is not bool
            or type(self.available_memory_bytes) is not int
            or self.available_memory_bytes < 0
            or any(
                type(v) not in (int, float) or not isfinite(v) or v < 0
                for v in (self.cpu_percent, self.deadline_remaining_ms)
            )
            or self.cpu_percent > 100
        ):
            raise ValueError("invalid light resource sample")

    # {
    #   責務: [reason: 更新を遅延すべき資源条件を固定理由で返す]
    #   処理: [host圧・CPU余力・memory・操作deadlineを順に照合する]
    #   引数: [budget: 有限の更新予算] 戻り値: [str|None: Noneだけがadmit]
    # }
    def reason(self, budget: LightAdaptationBudget) -> str | None:
        if self.pressure:
            return "resource_pressure"
        if self.cpu_percent + budget.max_cpu_percent > 100:
            return "cpu_pressure"
        if self.available_memory_bytes < budget.max_memory_bytes:
            return "memory_pressure"
        if self.deadline_remaining_ms <= budget.max_update_ms:
            return "inference_deadline"
        return None
