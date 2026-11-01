"""Mode/Phaseの不変Snapshotと一つの推論Stepの所有token。"""

from dataclasses import dataclass
import json
from pathlib import Path

from ai_game_player.runtime_mode_config import (
    PHASE_REQUIRED_OPERATION,
    DEFAULT_RUNTIME_MODE,
    RuntimeMode,
    RuntimeModeConfig,
    RuntimePhase,
    validate_runtime_identifier,
)


# {
#   責務: [RuntimeModeSnapshot: 利用者選択、実効Mode、Phaseと現在の操作世代を固定してSubsystemへ渡す]
#   フィールド: [selected_config: 利用者が選んだ宣言, effective_config: 現在適用した宣言,
#     phase: 現段階, version: metadata更新の単調版, epoch: 計算結果の採用を制限する世代,
#     pending_config/pending_reason: 次の安全境界で適用する要求,
#     degrade_reason: 実効Modeを軽くした理由, stop_requested: 明示再開まで保持する停止latch,
#     step_active/artifact_version: 現Stepの所有状態と読み取るArtifact版,
#     evaluation_version/evaluation_passed: PROMOTE段階へ入るための外部評価結果。昇格そのものではない,
#     default_mode: 宣言版1の既定値]
# }
@dataclass(frozen=True, slots=True)
class RuntimeModeSnapshot:
    selected_config: RuntimeModeConfig
    effective_config: RuntimeModeConfig
    phase: RuntimePhase = RuntimePhase.IDLE
    version: int = 0
    epoch: int = 0
    pending_config: RuntimeModeConfig | None = None
    pending_reason: str | None = None
    degrade_reason: str | None = None
    stop_requested: bool = False
    step_active: bool = False
    artifact_version: str | None = None
    evaluation_version: str | None = None
    evaluation_passed: bool = False
    default_mode: RuntimeMode = DEFAULT_RUNTIME_MODE

    # {
    #   責務: [mode: Subsystemが使う現在の実効Modeを返す]
    #   処理: [pendingや利用者選択ではなく適用済み宣言から取り出す]
    #   引数: [self: 対象Snapshot]
    #   戻り値: [RuntimeMode: 現在の実効方針]
    # }
    @property
    def mode(self) -> RuntimeMode:
        return self.effective_config.mode

    # {
    #   責務: [to_dict: Journalとcrash recovery用のmetadataをJSONへ投影する]
    #   処理: [Mode/Phase/版と停止・pending状態を含む独立辞書を作る]
    #   引数: [self: 対象Snapshot]
    #   戻り値: [dict: モデルや画面データを含まない状態metadata]
    # }
    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "selected_config": self.selected_config.to_dict(),
            "effective_config": self.effective_config.to_dict(),
            "default_mode": self.default_mode.value,
            "phase": self.phase.value,
            "version": self.version,
            "epoch": self.epoch,
            "pending_config": None
            if self.pending_config is None
            else self.pending_config.to_dict(),
            "pending_reason": self.pending_reason,
            "degrade_reason": self.degrade_reason,
            "stop_requested": self.stop_requested,
            "step_active": self.step_active,
            "artifact_version": self.artifact_version,
            "evaluation_version": self.evaluation_version,
            "evaluation_passed": self.evaluation_passed,
        }


# {
#   責務: [RuntimeStepToken: 一つのStepが開始時のMode世代とArtifact版を所有することを表す]
#   フィールド: [owner_id: Controller instanceのID, sequence: instance内Step番号,
#     epoch: 開始時の実効世代, run_generation: 既存RunControllerの再開世代, artifact_version: Step中に固定する版]
# }
@dataclass(frozen=True, slots=True)
class RuntimeStepToken:
    owner_id: str
    sequence: int
    epoch: int
    run_generation: int
    artifact_version: str


MAX_MODE_STATE_BYTES = 64 * 1024


# {
#   責務: [read_runtime_mode_checkpoint: Mode metadataを上限付きで読み込み検証する]
#   処理: [64KiBを超えるJSON、重複キー、版や型の不一致を拒否する]
#   引数: [path: 読み取るcheckpointファイル]
#   戻り値: [RuntimeModeSnapshot: 再開前の過去metadata]
#   エラー: [OSError/ValueError/UnicodeDecodeError: ファイル不明、破損、容量・契約違反]
# }
def read_runtime_mode_checkpoint(path: Path) -> RuntimeModeSnapshot:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_MODE_STATE_BYTES + 1)
    if len(raw) > MAX_MODE_STATE_BYTES:
        raise ValueError("mode checkpoint exceeds its byte bound")
    return _read_snapshot(json.loads(raw, object_pairs_hook=_unique_fields))


# {
#   責務: [_read_snapshot: 保存metadataを厳密に検証して復旧対象の値へ変換する]
#   処理: [フィールド・enum・型・能力とPhaseの整合性を照合する]
#   引数: [value: checkpoint JSONの値]
#   戻り値: [RuntimeModeSnapshot: 検証済みの過去状態。呼出側は安全IDLEへ置き換える]
#   エラー: [ValueError: 破損・版不一致・矛盾する状態]
# }
def _read_snapshot(value: object) -> RuntimeModeSnapshot:
    fields = set(
        RuntimeModeSnapshot(RuntimeModeConfig(), RuntimeModeConfig()).to_dict()
    )
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("runtime mode checkpoint fields are invalid")
    for key in ("version", "epoch"):
        if type(value[key]) is not int or not 0 <= value[key] < 1_000_000_000_000:
            raise ValueError("runtime mode checkpoint counter is invalid")
    for key in ("stop_requested", "step_active", "evaluation_passed"):
        if type(value[key]) is not bool:
            raise ValueError("runtime mode checkpoint flag is invalid")
    for key in (
        "pending_reason",
        "degrade_reason",
        "artifact_version",
        "evaluation_version",
    ):
        if value[key] is not None:
            validate_runtime_identifier(value[key], key)
    if value["default_mode"] != DEFAULT_RUNTIME_MODE.value:
        raise ValueError("runtime mode checkpoint default is invalid")
    selected = RuntimeModeConfig.from_dict(value["selected_config"])
    effective = RuntimeModeConfig.from_dict(value["effective_config"])
    pending = (
        None
        if value["pending_config"] is None
        else RuntimeModeConfig.from_dict(value["pending_config"])
    )
    phase = RuntimePhase(value["phase"])
    required = PHASE_REQUIRED_OPERATION.get(phase)
    if required is not None and required not in effective.operations(phase):
        raise ValueError("runtime mode checkpoint phase contradicts mode")
    if value["step_active"] and (
        phase not in {RuntimePhase.INFER, RuntimePhase.ERROR, RuntimePhase.STOPPING}
        or value["artifact_version"] is None
    ):
        raise ValueError("runtime mode checkpoint active step is invalid")
    if phase == RuntimePhase.PROMOTE and not (
        value["evaluation_passed"] and value["evaluation_version"] is not None
    ):
        raise ValueError("runtime mode checkpoint promotion lacks evaluation")
    return RuntimeModeSnapshot(
        selected,
        effective,
        phase,
        value["version"],
        value["epoch"],
        pending,
        value["pending_reason"],
        value["degrade_reason"],
        value["stop_requested"],
        value["step_active"],
        value["artifact_version"],
        value["evaluation_version"],
        value["evaluation_passed"],
    )


# {
#   責務: [_unique_fields: checkpointのJSON重複キーを拒否する]
#   処理: [同名フィールドを上書きで解釈せず破損として扱う]
#   引数: [pairs: JSON objectのkey/value一覧]
#   戻り値: [dict: 重複のない辞書]
#   エラー: [ValueError: 同じキーが二回現れる]
# }
def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("runtime mode checkpoint has duplicate fields")
        result[key] = value
    return result
