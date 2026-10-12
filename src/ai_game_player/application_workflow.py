import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ai_game_player.config import AppConfig, ConfigStore
from ai_game_player.game_session import SessionRuntime
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment
from ai_game_player.runtime.session_composition import RuntimeComposition, SessionRuntimeConfiguration
from ai_game_player.runtime_log import RuntimeLog
from ai_game_player.run_control import RunController


# {
#   責務: [MemorySource: 同じSession Runtimeへ最新の画面観測と操作候補を渡す]
#   フィールド: [observation: 判断対象の画面, candidates: 画面に対する操作候補]
# }
class MemorySource:
    # {
    #   責務: [__init__: Session Pipelineが読む観測と候補の初期値を用意する]
    #   処理: [観測がない状態を明示して初期化する]
    #   引数: []
    #   戻り値: []
    # }
    def __init__(self) -> None:
        self.observation: ScreenObservation | None = None
        self.candidates: list[ActionCandidate] = []

    # {
    #   責務: [update: Pipelineが次に読む観測と候補を同時に登録する]
    #   処理: [同じUI stepから受け取った観測と候補を一組で保持する]
    #   引数: [observation: 操作判断の対象となる画面, candidates: 画面から抽出した操作候補]
    #   戻り値: []
    # }
    def update(self, observation: ScreenObservation, candidates: list[ActionCandidate]) -> None:
        self.observation = observation
        self.candidates = candidates

    # {
    #   責務: [read: Session Pipelineへ最後に登録した観測と候補を返す]
    #   処理: [観測未登録なら判断を開始できるよう呼び出し元へ理由を示す]
    #   引数: []
    #   戻り値: [tuple[ScreenObservation, list[ActionCandidate]]: 判断対象と同じ画面から得た候補]
    #   エラー: [RuntimeError: Session開始前に観測が登録されていない場合]
    # }
    def read(self) -> tuple[ScreenObservation, list[ActionCandidate]]:
        if self.observation is None:
            raise RuntimeError("画面観測をsessionへ登録してからPipelineを実行してください")
        return self.observation, self.candidates


# {
#   責務: [ApplicationSettings: UIからstep開始時に確定したProviderと操作設定を保持する]
#   フィールド: [provider/model/endpoint: 判断Provider設定, personality/purpose: 判断条件, live_execution: 実入力許可, input_mode: 入力方式]
# }
@dataclass(frozen=True)
class ApplicationSettings:
    provider: str
    model: str
    endpoint: str
    personality: str
    purpose: str
    live_execution: bool
    input_mode: str


# {
#   責務: [PreparedObservation: 候補評価に成功した画面と候補をUIへ返す]
#   フィールド: [observation: Pipelineが判断する画面, candidates: 同画面の操作候補, evaluation: 候補検査結果]
# }
@dataclass(frozen=True)
class PreparedObservation:
    observation: ScreenObservation
    candidates: list[ActionCandidate]
    evaluation: list[dict[str, object]]


# {
#   責務: [PreparedSessionStep: 検証済みstep設定と観測評価をUIへ返す]
#   フィールド: [observation: stepが判断する画面, candidates: 同画面の操作候補, evaluation: 候補検査結果, dry_run: 実入力を行わないか]
# }
@dataclass(frozen=True)
class PreparedSessionStep:
    observation: ScreenObservation
    candidates: list[ActionCandidate]
    evaluation: list[dict[str, object]]
    dry_run: bool


# {
#   責務: [ApplicationWorkflowService: UIに依存せずstep入力を保存・検証しSession sourceを更新する]
#   フィールド: [config_store: 利用者設定の保存先, runtime_composition: 候補評価を行うruntime composition, memory_source: Session Pipelineが読む観測source]
# }
class ApplicationWorkflowService:
    # {
    #   責務: [__init__: UI非依存のstep workflowに保存先と実行依存を注入する]
    #   処理: [設定保存、候補評価、Session source更新に必要な依存だけを保持する]
    #   引数: [config_store: 利用者設定の保存先, runtime_composition: 候補評価を提供するcomposition root, memory_source: 検証した観測と候補の登録先]
    #   戻り値: []
    # }
    def __init__(
        self,
        config_store: ConfigStore,
        runtime_composition: RuntimeComposition,
        memory_source: MemorySource,
    ) -> None:
        self.config_store = config_store
        self.runtime_composition = runtime_composition
        self.memory_source = memory_source

    # {
    #   責務: [assess_outcome: provider設定に従って画面の進行・成功・失敗を評価する]
    #   処理: [RuntimeCompositionのoutcome evaluatorへ同一Sessionの現画面と前画面を渡す]
    #   引数: [provider_name/model/endpoint: 評価時のProvider設定, observation: 現在の画面, previous: 比較対象となる直前の画面]
    #   戻り値: [OutcomeAssessment: 状態と評価根拠]
    #   エラー: [RuntimeError: 選択Providerの接続または評価に失敗した場合]
    # }
    def assess_outcome(
        self,
        provider_name: str,
        model: str,
        endpoint: str,
        observation: ScreenObservation,
        previous: ScreenObservation | None,
    ) -> OutcomeAssessment:
        return self.runtime_composition.assess_outcome(
            provider_name,
            model,
            endpoint,
            observation,
            previous,
        )

    # {
    #   責務: [assess_locally: Provider依存のないruleで画面のterminal状態を評価する]
    #   処理: [共有OutcomeEvaluatorのlocal判定へ画面観測を渡す]
    #   引数: [observation: terminal状態を調べる画面]
    #   戻り値: [OutcomeAssessment: local ruleの評価結果]
    # }
    def assess_locally(self, observation: ScreenObservation) -> OutcomeAssessment:
        return self.runtime_composition.assess_locally(observation)

    # {
    #   責務: [list_models: provider endpointから利用可能な判断model名を取得する]
    #   処理: [RuntimeCompositionのmodel catalog境界へendpointを渡す]
    #   引数: [endpoint: model catalogを問い合わせるOllama endpoint]
    #   戻り値: [list[str]: providerが利用可能と返したmodel名]
    #   エラー: [RuntimeError: providerへ接続できないか応答を解析できない場合]
    # }
    def list_models(self, endpoint: str) -> list[str]:
        return self.runtime_composition.list_models(endpoint)

    # {
    #   責務: [create_session_runtime: step snapshotからsession-scoped providerとpipelineを構築する]
    #   処理: [ApplicationSettingsとtarget設定をruntime contractへ変換し、候補sourceを注入する]
    #   引数: [settings: session開始時に確定したProviderと入力方式, game_directory: sessionデータ保存先, dry_run: OS実入力を禁止するか, window_handle: 入力先HWND, window_process_id: 入力先プロセスID, controller: pipelineが停止状態を確認するrun controller, cursor_callback: 自動カーソル移動の通知先, runtime_log: session eventを記録するlogger]
    #   戻り値: [SessionRuntime: Session Controllerがstep間で保持するProviderとPipeline]
    # }
    def create_session_runtime(
        self,
        settings: ApplicationSettings,
        *,
        game_directory: Path,
        dry_run: bool,
        window_handle: int | None,
        window_process_id: int | None,
        controller: RunController,
        cursor_callback: Callable[[tuple[int, int] | None, bool], None],
        runtime_log: RuntimeLog,
    ) -> SessionRuntime:
        configuration = SessionRuntimeConfiguration(
            provider_name=settings.provider,
            model=settings.model,
            endpoint=settings.endpoint,
            game_directory=game_directory,
            dry_run=dry_run,
            window_handle=window_handle,
            input_mode=settings.input_mode,
            window_process_id=window_process_id,
        )
        return self.runtime_composition.create_session_runtime(
            configuration,
            source=self.memory_source,
            controller=controller,
            automated_cursor_position_callback=cursor_callback,
            runtime_log=runtime_log,
        )

    # {
    #   責務: [prepare_step: UI入力を保存・解析し安全評価済みstepとしてSession sourceへ登録する]
    #   処理: [設定保存後に観測と候補JSONを型へ変換し、候補理由を評価してsourceを更新する]
    #   引数: [settings: step開始時に確定したProvider・人格・入力設定, observation_json: UIが表示する観測JSON, candidates_json: UIが受け取った候補JSON, command: decisionまたはexecute]
    #   戻り値: [PreparedSessionStep: Session ControllerとUIが共有する検証結果]
    #   エラー: [ValueErrorまたはJSONDecodeError: command、観測JSON、候補JSONが不正な場合]
    # }
    def prepare_step(
        self,
        settings: ApplicationSettings,
        observation_json: str,
        candidates_json: str,
        command: str,
    ) -> PreparedSessionStep:
        if command not in {"decision", "execute"}:
            raise ValueError(f"Unsupported session command: {command}")

        self.config_store.save(
            AppConfig(
                settings.provider,
                settings.model,
                settings.endpoint,
                settings.personality,
                settings.purpose,
                settings.live_execution,
                settings.input_mode,
            )
        )
        observation = ScreenObservation(**json.loads(observation_json))
        prepared_observation = self.update_observation(observation, candidates_json)
        return PreparedSessionStep(
            observation=prepared_observation.observation,
            candidates=prepared_observation.candidates,
            evaluation=prepared_observation.evaluation,
            dry_run=command != "execute" or not settings.live_execution,
        )

    # {
    #   責務: [update_observation: 画面観測と候補JSONを評価しSession Pipeline sourceを更新する]
    #   処理: [候補配列を検証してから安全評価を行い、成功後にだけ観測と候補をsourceへ登録する]
    #   引数: [observation: candidate座標と判断対象になる画面, candidates_json: 同じ画面上の操作候補JSON配列]
    #   戻り値: [PreparedObservation: Pipelineへ登録した観測、候補、候補評価結果]
    #   エラー: [ValueErrorまたはJSONDecodeError: 候補JSONが配列でないか候補schemaが不正な場合]
    # }
    def update_observation(
        self,
        observation: ScreenObservation,
        candidates_json: str,
    ) -> PreparedObservation:
        candidate_values = json.loads(candidates_json)
        if not isinstance(candidate_values, list):
            raise ValueError("操作候補JSONには候補の配列を指定してください")
        candidates = [ActionCandidate.from_dict(item) for item in candidate_values]
        evaluation = self.runtime_composition.explain_actions(observation, candidates)
        self.memory_source.update(observation, candidates)
        return PreparedObservation(
            observation=observation,
            candidates=candidates,
            evaluation=evaluation,
        )
