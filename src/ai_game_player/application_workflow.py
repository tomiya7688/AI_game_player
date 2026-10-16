import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ai_game_player.candidate_merger import CandidateMerger
from ai_game_player.capture_region import CaptureRegion
from ai_game_player.config import AppConfig, ConfigStore
from ai_game_player.game_session import SessionRuntime
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.ocr_detector import OcrTextCandidateDetector
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
#   フィールド: [provider/model/endpoint: 判断Provider設定, personality/purpose: 判断条件, live_execution: 実入力許可, input_mode: 入力方式, capture_region: capture sourceとscreen/window ROI設定]
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
    capture_region: CaptureRegion = CaptureRegion()


# {
#   責務: [PreparedObservation: 候補評価に成功した画面と候補をUIへ返す]
#   フィールド: [observation: Pipelineが判断する画面, candidates: 同画面で統合した操作候補, evaluation: 候補検査結果と入力元]
# }
@dataclass(frozen=True)
class PreparedObservation:
    observation: ScreenObservation
    candidates: list[ActionCandidate]
    evaluation: list[dict[str, object]]


# {
#   責務: [PreparedSessionStep: 検証済みstep設定と観測評価をUIへ返す]
#   フィールド: [observation: stepが判断する画面, candidates: 同画面で統合した操作候補, evaluation: 候補検査結果と入力元, dry_run: 実入力を行わないか]
# }
@dataclass(frozen=True)
class PreparedSessionStep:
    observation: ScreenObservation
    candidates: list[ActionCandidate]
    evaluation: list[dict[str, object]]
    dry_run: bool


# {
#   責務: [ApplicationWorkflowService: 画面と候補を検証・評価し、次の判断に使う観測sourceを更新する]
#   フィールド: [config_store: 利用者設定の保存先, runtime_composition: 候補の安全評価を行う実行構成, memory_source: 判断処理が読む画面と候補の保存先, candidate_merger: 自動操作・OCR・画像候補を重複統合する処理, ocr_detector: 認識文字を操作候補へ変換する処理]
# }
class ApplicationWorkflowService:
    # {
    #   責務: [__init__: GUIを使わない候補処理へ設定保存・評価・観測保存の依存先を接続する]
    #   処理: [自動操作・OCR・画像候補の統合と評価に使う処理を生成して保持する]
    #   引数: [config_store: 利用者設定の保存先, runtime_composition: 候補の安全評価を提供する実行構成, memory_source: 検証済み画面と候補の登録先]
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
        self.candidate_merger = CandidateMerger()
        self.ocr_detector = OcrTextCandidateDetector()

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
    #   責務: [prepare_step: 利用者設定を保存し、画面取得結果と操作候補を次の判断に登録する]
    #   処理: [判断元・人格・実入力・取得範囲の設定を保存し、認識文字・画像・任意の自動操作候補を統合して安全評価する]
    #   引数: [settings: step開始時に確定したProvider・人格・入力・capture region設定, observation_json: UIが表示する観測JSON, candidates_json: UIが受け取った候補JSON, command: decisionまたはexecute]
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
                settings.capture_region,
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
    #   責務: [update_observation: 画面候補を安全評価して判断処理の観測保存先を更新する]
    #   処理: [任意の自動操作候補JSONと画面内の認識文字・画像候補を統合し、評価成功後に画面と候補を同時に登録する]
    #   引数: [observation: 操作位置と画面状態を含む取得結果, candidates_json: 同じ画面上で利用する任意の自動操作候補]
    #   戻り値: [PreparedObservation: 判断処理へ登録した画面、統合候補、安全評価結果]
    #   エラー: [ValueErrorまたはJSONDecodeError: 候補JSONが配列でないか候補schemaが不正な場合]
    # }
    def update_observation(
        self,
        observation: ScreenObservation,
        candidates_json: str,
    ) -> PreparedObservation:
        prepared = self._prepare_observation(observation, candidates_json)
        self.memory_source.update(observation, prepared.candidates)
        return prepared

    # {
    #   責務: [preview_observation: 画面候補を統合・評価し、判断処理の画面と候補を変更せずGUI previewを返す]
    #   処理: [update_observationと同じ候補統合・評価を行い、実行中の判断処理が参照する保存先は更新しない]
    #   引数: [observation: 操作位置と画面状態を含む取得結果, candidates_json: 同じ画面上で利用する任意の自動操作候補]
    #   戻り値: [PreparedObservation: GUIへ表示する統合候補と入力元付き評価結果]
    #   エラー: [ValueErrorまたはJSONDecodeError: 候補JSONまたはObservation featureが不正な場合]
    # }
    def preview_observation(
        self,
        observation: ScreenObservation,
        candidates_json: str,
    ) -> PreparedObservation:
        return self._prepare_observation(observation, candidates_json)

    # {
    #   責務: [_prepare_observation: 自動操作・認識文字・画像の候補を統合して安全評価する]
    #   処理: [同じ画面から得た候補だけを統合し、各候補の入力元を記録した評価結果を作る]
    #   引数: [observation: 操作位置と画面状態を含む取得結果, candidates_json: 同じ画面上で利用する任意の自動操作候補]
    #   戻り値: [PreparedObservation: 統合候補と入力元付き評価結果]
    #   エラー: [ValueErrorまたはJSONDecodeError: 候補JSONまたはObservation featureが不正な場合]
    # }
    def _prepare_observation(
        self,
        observation: ScreenObservation,
        candidates_json: str,
    ) -> PreparedObservation:
        sourced_candidates = self._merge_observation_candidates(observation, candidates_json)
        candidates = [candidate for candidate, _sources in sourced_candidates]
        source_by_action_id = {
            candidate.action_id: list(sources)
            for candidate, sources in sourced_candidates
        }
        evaluation = self.runtime_composition.explain_actions(observation, candidates)
        evaluation_with_sources = [
            {
                **result,
                "sources": source_by_action_id.get(str(result.get("action_id", "")), []),
            }
            for result in evaluation
        ]
        return PreparedObservation(
            observation=observation,
            candidates=candidates,
            evaluation=evaluation_with_sources,
        )

    # {
    #   責務: [_merge_observation_candidates: 自動操作・認識文字・画像から得た候補を一つの一覧へまとめる]
    #   処理: [各候補の形式を検証し、認識文字を共通要素へ変換して重複統合器へ渡す]
    #   引数: [observation: 画面解析が検出した認識文字・画像候補を持つ画面, candidates_json: 任意の自動操作候補JSON配列]
    #   戻り値: [list[tuple[ActionCandidate, tuple[str, ...]]]: 代表操作候補と自動操作・認識文字・画像の入力元]
    #   エラー: [ValueErrorまたはJSONDecodeError: 候補JSONまたは画面内候補の形式が不正な場合]
    # }
    def _merge_observation_candidates(
        self,
        observation: ScreenObservation,
        candidates_json: str,
    ) -> list[tuple[ActionCandidate, tuple[str, ...]]]:
        candidate_values = json.loads(candidates_json)
        if not isinstance(candidate_values, list):
            raise ValueError("操作候補JSONには候補の配列を指定してください")
        automation_candidates = [ActionCandidate.from_dict(item) for item in candidate_values]
        raw_ocr_candidates = observation.features.get("ocr_candidates", [])
        raw_image_candidates = observation.features.get("image_candidates", [])
        if not isinstance(raw_ocr_candidates, list):
            raise ValueError("ObservationのOCR候補には候補の配列を指定してください")
        if not isinstance(raw_image_candidates, list):
            raise ValueError("Observationの画像候補には候補の配列を指定してください")
        if any(not isinstance(item, dict) for item in raw_ocr_candidates):
            raise ValueError("ObservationのOCR候補にはオブジェクトだけを指定してください")
        if any(not isinstance(item, dict) for item in raw_image_candidates):
            raise ValueError("Observationの画像候補にはオブジェクトだけを指定してください")
        ocr_elements = self.ocr_detector.detect(observation, raw_ocr_candidates)
        image_candidates = [ActionCandidate.from_dict(item) for item in raw_image_candidates]
        return self.candidate_merger.merge_with_sources(
            automation_candidates,
            ocr_elements,
            image_candidates,
        )
