from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Protocol


PR_CAMPAIGN_DURATION_SECONDS = 600.0
RELEASE_CAMPAIGN_DURATION_SECONDS = 1800.0
DEFAULT_CAMPAIGN_MAX_STEPS = 10_000
DEFAULT_TERMINAL_RESTART_LIMIT = 100
DEFAULT_RECOVERY_LIMIT = 2
DEFAULT_STALL_ACTION_LIMIT = 8
CAMPAIGN_ERROR_DETAIL_LIMIT = 500
CAMPAIGN_FAILURE_CATEGORIES = frozenset(
    {
        "safety_block",
        "recognition_failure",
        "capture_failure",
        "execution_failure",
        "decision_failure",
        "episode_start_failure",
        "live_input_not_verified",
        "terminal_restart_limit",
        "restart_failed",
        "stall_limit",
        "step_limit",
        "unexpected_failure",
        "cleanup_failure",
        "campaign_configuration",
        "artifact_write_failure",
        "milestone_failure",
    }
)


@dataclass(frozen=True)
# {
#   責務: [CampaignState: 1回のscreen observationから得た状態照合値、終了状態、score、最大tileを保持する]
#   フィールド: [signature: 同一状態を比較するscreen-derived署名, terminal: ゲーム終了を検出した状態, score: 現在score。未認識ならNone, max_tile: 盤面の最大tile。盤面未認識ならNone]
# }
class CampaignState:
    signature: str
    terminal: bool
    score: int | None = None
    max_tile: int | None = None

    # {
    #   責務: [__post_init__: campaign状態の必須signatureと数値metricを検証する]
    #   引数: [self: 検証するscreen-derived状態]
    #   戻り値: []
    #   エラー: [ValueError: signatureが空、terminalがboolでない、またはmetricが非負整数でない場合]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.signature, str) or not self.signature.strip():
            raise ValueError("campaign state signature must not be empty")
        if type(self.terminal) is not bool:
            raise ValueError("campaign terminal state must be bool")
        if self.score is not None and (type(self.score) is not int or self.score < 0):
            raise ValueError("campaign score must be a non-negative integer or None")
        if self.max_tile is not None and (type(self.max_tile) is not int or self.max_tile < 0):
            raise ValueError("campaign maximum tile must be a non-negative integer or None")


@dataclass(frozen=True)
# {
#   責務: [CampaignActionResult: campaign episodeが実行した操作ID、OS入力の実行状態、実行方式を返す]
#   フィールド: [action_id: 実行候補のID, executed: executorが入力を送信した状態, mode: liveまたはdry-run等の実行方式, detail: executorが返した説明]
# }
class CampaignActionResult:
    action_id: str
    executed: bool
    mode: str
    detail: str = ""

    # {
    #   責務: [__post_init__: campaign操作結果のID、実行flag、modeを検証する]
    #   引数: [self: 検証するexecutor結果]
    #   戻り値: []
    #   エラー: [ValueError: action IDまたはmodeが空、またはexecutedがboolでない場合]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("campaign action ID must not be empty")
        if type(self.executed) is not bool:
            raise ValueError("campaign action executed flag must be bool")
        if not isinstance(self.mode, str) or not self.mode.strip():
            raise ValueError("campaign action mode must not be empty")


@dataclass(frozen=True)
# {
#   責務: [CampaignConfig: PR・Release profile共通のduration、step上限、terminal restart、stall recovery条件を保持する]
#   フィールド: [profile: 実行目的を示す名前, duration_seconds: campaignを続ける目標時間, max_steps: 1runで許可するaction数上限, terminal_restart_limit: game-over後に許可する再起動回数, recovery_limit: stall後に許可するbrowser episode再生成回数, stall_action_limit: 同じscreen状態が続いたときrecoveryするaction数, step_delay_seconds: 実入力後に次の画面を読むまでの待機時間]
# }
class CampaignConfig:
    profile: str
    duration_seconds: float
    max_steps: int = DEFAULT_CAMPAIGN_MAX_STEPS
    terminal_restart_limit: int = DEFAULT_TERMINAL_RESTART_LIMIT
    recovery_limit: int = DEFAULT_RECOVERY_LIMIT
    stall_action_limit: int = DEFAULT_STALL_ACTION_LIMIT
    step_delay_seconds: float = 0.05

    # {
    #   責務: [__post_init__: duration、上限値、待機時間の有限性と許容範囲を検証する]
    #   引数: [self: 検証するcampaign profile]
    #   戻り値: []
    #   エラー: [ValueError: durationが正でない、上限が整数でない、または待機時間が有限の非負値でない場合]
    # }
    def __post_init__(self) -> None:
        if not isinstance(self.profile, str) or not self.profile.strip():
            raise ValueError("campaign profile must not be empty")
        if not _is_finite_number(self.duration_seconds) or self.duration_seconds <= 0:
            raise ValueError("campaign duration must be a finite positive number")
        if type(self.max_steps) is not int or self.max_steps < 1:
            raise ValueError("campaign max_steps must be a positive integer")
        for name, value in (
            ("terminal_restart_limit", self.terminal_restart_limit),
            ("recovery_limit", self.recovery_limit),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"campaign {name} must be a non-negative integer")
        if type(self.stall_action_limit) is not int or self.stall_action_limit < 1:
            raise ValueError("campaign stall_action_limit must be a positive integer")
        if not _is_finite_number(self.step_delay_seconds) or self.step_delay_seconds < 0:
            raise ValueError("campaign step delay must be a finite non-negative number")


# {
#   責務: [CampaignFailure: campaign runnerが停止する分類codeと読者向け理由を保持する]
#   フィールド: [category: reportへ保存する既知failure分類, detail: 失敗した対象と原因の説明]
# }
class CampaignFailure(RuntimeError):
    # {
    #   責務: [__init__: 定義済みfailure categoryと具体的な失敗理由を例外へ保持する]
    #   引数: [category: campaign reportで機械判定するfailure code, detail: 対象operationと失敗理由]
    #   戻り値: []
    #   エラー: [ValueError: categoryがcampaign failure分類表にない場合]
    # }
    def __init__(self, category: str, detail: str) -> None:
        if category not in CAMPAIGN_FAILURE_CATEGORIES:
            raise ValueError(f"unknown campaign failure category: {category}")
        super().__init__(detail)
        self.category = category
        self.detail = detail


class CampaignEpisode(Protocol):
    # {
    #   責務: [observe: 画面由来の状態signature、終了状態、score、最大tileを返す]
    #   引数: [なし]
    #   戻り値: [CampaignState: 同じ時点のscreenから読んだcampaign状態]
    #   エラー: [CampaignFailure: captureまたはscreen recognitionに失敗した場合]
    # }
    def observe(self) -> CampaignState:
        ...

    # {
    #   責務: [act: 現在状態で許可された候補を判断器とSafety経路へ渡し、1操作を実行する]
    #   引数: [なし。実行対象のObservationは直前のobserve結果をepisodeが保持する]
    #   戻り値: [CampaignActionResult: 選択action ID、live実行状態、executor mode]
    #   エラー: [CampaignFailure: safety、decision、OS inputの実行に失敗した場合]
    # }
    def act(self) -> CampaignActionResult:
        ...

    # {
    #   責務: [record_outcome: screen変化を今回のSafety評価へ返し次actionのreliability evidenceにする]
    #   引数: [status: ongoing・failure・unknown等の観測結果分類, confidence: screen evidenceの確信度, evidence: statusを裏付けるscreen変化またはterminal説明]
    #   戻り値: []
    #   エラー: [RuntimeError: 対応する操作のSafety評価がない場合]
    # }
    def record_outcome(self, status: str, confidence: float, evidence: str) -> None:
        ...

    # {
    #   責務: [close: episode固有のwindow capture、input executor、browser processを終了する]
    #   引数: [なし]
    #   戻り値: []
    #   エラー: [Exception: episodeのresourceを解放できない場合]
    # }
    def close(self) -> None:
        ...


def _is_finite_number(value: object) -> bool:
    # {
    #   責務: [_is_finite_number: bool・文字列・float overflowを除き、finiteな整数または小数を検証する]
    #   引数: [value: durationまたはsettle時間として設定された任意の値]
    #   戻り値: [bool: time arithmeticに安全なfinite numberならtrue]
    # }
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
