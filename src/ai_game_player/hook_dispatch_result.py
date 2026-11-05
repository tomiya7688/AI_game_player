"""1回のHook dispatchの結果と、Hookごとの実行記録（latency・失敗分類）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_game_player.stage_envelope import StageEnvelope


# Hookごとの結果status。completed以外はHookの出力を一切使っていない。
HOOK_STATUS_COMPLETED = "completed"
HOOK_STATUS_FAILED = "failed"
HOOK_STATUS_TIMEOUT = "timeout"
HOOK_STATUS_QUARANTINED = "quarantined"


# {
#   責務: [HookExecutionRecord: 1つのHookを1回呼んだ結果を、payloadを含まない監査用の値として保持する]
#   フィールド: [
#     hook_id/mode: 対象Hookと種類
#     status: completed・failed・timeout・quarantined（以前のtimeout等で呼び出し対象外）のいずれか
#     elapsed_ns: 呼び出し開始から結果確定までの単調時計の差（ns）。呼ばなかった場合は0
#     failure_kind: 失敗の分類（exception・timeout・invalid_output・contract_violation・capability_denied・quarantined）。成功時はNone
#     error_code: 出力の再検証で得たStageEnvelopeErrorのcode（malformed等）、またはaccess_policyの拒否code。該当しない場合はNone
#     output_envelope_ids: 採用したHook出力EnvelopeのID
#   ]
# }
@dataclass(frozen=True)
class HookExecutionRecord:
    hook_id: str
    mode: str
    status: str
    elapsed_ns: int
    failure_kind: str | None = None
    error_code: str | None = None
    output_envelope_ids: tuple[str, ...] = ()

    # {
    #   責務: [to_dict: 実行記録をJSON化できる辞書へ変換する]
    #   処理: [全fieldを素の値とlistで返す]
    #   引数: [self: 記録]
    #   戻り値: [dict[str, Any]: 監査・診断用の辞書]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "mode": self.mode,
            "status": self.status,
            "elapsed_ns": self.elapsed_ns,
            "failure_kind": self.failure_kind,
            "error_code": self.error_code,
            "output_envelope_ids": list(self.output_envelope_ids),
        }


# {
#   責務: [HookDispatchResult: 1つのstage境界でのHook dispatchの最終Envelope・派生Envelope・停止判定・計測値をまとめる]
#   フィールド: [
#     envelope: modify Hookを順に適用した後の検証済みEnvelope（modifyがなければ入力そのもの）
#     injected: inject Hookが追加した検証済み派生Envelope（実行順）
#     records: 呼び出し順のHookExecutionRecord
#     halted: on_failure=HALTのHookが失敗したか。Trueなら呼び出し側は後段へ進まず、入力なしで停止する
#     halt_reasons: 停止の原因（`<hook_id>:<failure_kind>`）
#     elapsed_ns: dispatch全体の単調時計の差（ns）
#   ]
# }
@dataclass(frozen=True)
class HookDispatchResult:
    envelope: StageEnvelope
    injected: tuple[StageEnvelope, ...] = ()
    records: tuple[HookExecutionRecord, ...] = ()
    halted: bool = False
    halt_reasons: tuple[str, ...] = ()
    elapsed_ns: int = 0

    # {
    #   責務: [to_dict: dispatch結果をpayloadを含まない診断用の辞書へ変換する]
    #   処理: [最終・派生EnvelopeはIDだけにし、実行記録を辞書化する]
    #   引数: [self: 結果]
    #   戻り値: [dict[str, Any]: 監査・診断用の辞書]
    # }
    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope.envelope_id,
            "stage": self.envelope.stage,
            "boundary": self.envelope.boundary,
            "injected_envelope_ids": [item.envelope_id for item in self.injected],
            "records": [record.to_dict() for record in self.records],
            "halted": self.halted,
            "halt_reasons": list(self.halt_reasons),
            "elapsed_ns": self.elapsed_ns,
        }
