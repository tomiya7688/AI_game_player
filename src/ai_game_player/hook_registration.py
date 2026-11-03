"""Processing Hookの登録内容（種類・接続点・順序・予算・失敗時方針）を表す契約。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ai_game_player.stage_envelope import (
    STAGE_BOUNDARIES,
    STAGE_ENVELOPE_MAX_ENCODED_BYTES,
    StageEnvelope,
)


# StageEnvelopeのproducer_idと同じ字面。Hook IDはそのままhook出力のproducer_idになる。
_HOOK_ID_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_HOOK_VERSION_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
HOOK_MAX_TIMEOUT_NS = 1_000_000_000
HOOK_DEFAULT_TIMEOUT_NS = 50_000_000
HOOK_DEFAULT_MAX_OUTPUT_BYTES = 65_536
HOOK_MAX_INJECTED_PER_HOOK = 16
HOOK_PRIORITY_RANGE = (-1000, 1000)


# {
#   責務: [HookMode: Hookがstage境界のEnvelopeへ行える操作の種類を列挙する]
#   フィールド: [READ: 観測だけで戻り値を持たない, MODIFY: 同じ境界の新しいEnvelopeを1件返す, INJECT: 派生Envelopeを0件以上追加する]
# }
class HookMode(str, Enum):
    READ = "read"
    MODIFY = "modify"
    INJECT = "inject"


# {
#   責務: [HookFailurePolicy: Hookの例外・timeout・不正出力をstage境界がどう扱うかを列挙する]
#   フィールド: [SKIP: 失敗したHookの出力だけを捨てて残りのHookと後段を続ける,
#     HALT: dispatch結果をhaltedにし、呼び出し側が入力なしで安全停止する]
# }
class HookFailurePolicy(str, Enum):
    SKIP = "skip"
    HALT = "halt"


HookCallback = Callable[[StageEnvelope], Any]


# {
#   責務: [HookRegistration: 1つのHookの識別・接続点・実行順・予算・失敗時方針を検証済みの不変値として保持する]
#   フィールド: [
#     hook_id/hook_version: 監査用ID（出力Envelopeのproducer_id/producer_versionになる）と版
#     mode: read/modify/injectのいずれか
#     stage/boundary: 接続するStageEnvelopeのstage IDとinput/output
#     callback: Envelopeを1件受け取る呼び出し。readはNone、modifyはStageEnvelope、injectはStageEnvelopeの列を返す
#     priority: 同じmodeの中で小さい順に実行する整数（同値はhook_id昇順）
#     timeout_ns: 1回の呼び出しの壁時計上限（ns）
#     max_output_bytes: 出力Envelope 1件のcanonical JSON byte上限
#     max_injected: injectが1回に追加できるEnvelope件数の上限
#     on_failure: 失敗時方針。省略時はmodifyがHALT、read/injectがSKIP
#   ]
# }
@dataclass(frozen=True)
class HookRegistration:
    hook_id: str
    hook_version: str
    mode: HookMode
    stage: str
    boundary: str
    callback: HookCallback
    priority: int = 0
    timeout_ns: int = HOOK_DEFAULT_TIMEOUT_NS
    max_output_bytes: int = HOOK_DEFAULT_MAX_OUTPUT_BYTES
    max_injected: int = 4
    on_failure: HookFailurePolicy | None = None

    # {
    #   責務: [__post_init__: 登録値の字面と予算が有限で一貫しているか検証し、失敗時方針の既定値を確定する]
    #   処理: [
    #     1: hook_id/hook_versionの字面、mode・boundary・失敗時方針の列挙値、callbackの呼び出し可能性を確認する
    #     2: priority・timeout・byte上限・inject件数が許容範囲の整数か確認する
    #     3: on_failure未指定なら、modifyはHALT（加工を前提とする後段へ未加工dataを黙って渡さない）、他はSKIPにする
    #   ]
    #   引数: [self: 検証対象の登録]
    #   戻り値: [なし]
    #   エラー: [ValueError: 字面・列挙値・範囲のいずれかが不正な場合]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.hook_id, str) or not _HOOK_ID_PATTERN.fullmatch(
            self.hook_id
        ):
            raise ValueError("hook_id must be a lowercase namespaced id")
        if not isinstance(
            self.hook_version, str
        ) or not _HOOK_VERSION_PATTERN.fullmatch(self.hook_version):
            raise ValueError("hook_version must be a short version token")
        if not isinstance(self.mode, HookMode):
            raise ValueError("mode must be a HookMode")
        if not isinstance(self.stage, str) or not self.stage:
            raise ValueError("stage must be a stage id")
        if self.boundary not in STAGE_BOUNDARIES:
            raise ValueError("boundary must be input or output")
        if not callable(self.callback):
            raise ValueError("callback must be callable")
        _require_int_range(self.priority, "priority", *HOOK_PRIORITY_RANGE)
        _require_int_range(self.timeout_ns, "timeout_ns", 1, HOOK_MAX_TIMEOUT_NS)
        _require_int_range(
            self.max_output_bytes,
            "max_output_bytes",
            1,
            STAGE_ENVELOPE_MAX_ENCODED_BYTES,
        )
        _require_int_range(
            self.max_injected, "max_injected", 0, HOOK_MAX_INJECTED_PER_HOOK
        )
        if self.on_failure is None:
            default = (
                HookFailurePolicy.HALT
                if self.mode is HookMode.MODIFY
                else HookFailurePolicy.SKIP
            )
            object.__setattr__(self, "on_failure", default)
        elif not isinstance(self.on_failure, HookFailurePolicy):
            raise ValueError("on_failure must be a HookFailurePolicy")

    # {
    #   責務: [order_key: 同じmode内の決定論的な実行順を返す]
    #   処理: [priority、hook_idの順で比較できる組を作る]
    #   引数: [self: 登録]
    #   戻り値: [tuple[int, str]: 昇順に並べると実行順になる鍵]
    # }
    def order_key(self) -> tuple[int, str]:
        return (self.priority, self.hook_id)


# {
#   責務: [_require_int_range: 予算値がboolでない整数で、閉区間内にあるか確認する]
#   処理: [型と範囲を検査する]
#   引数: [value: 検査値, name: エラー文のfield名, minimum/maximum: 許容する最小値・最大値]
#   戻り値: [なし]
#   エラー: [ValueError: 整数でないか範囲外の場合]
# }
def _require_int_range(value: Any, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
