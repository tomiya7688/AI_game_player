"""Application-independent factories for long-lived session components."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_game_player.action_executor import ExecutionResult
from ai_game_player.evaluator import ActionEvaluator
from ai_game_player.event_journal import EventJournal
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.outcome import OutcomeAssessment, OutcomeEvaluator
from ai_game_player.observation_source import ObservationSource
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import OllamaProvider, RuleProvider
from ai_game_player.run_control import RunController
from ai_game_player.runtime_log import RuntimeLog


@dataclass(frozen=True)
# {
#   責務: [SessionRuntimeConfiguration: セッション開始時に確定するProvider・保存先・入力対象設定を保持する]
#   フィールド: [provider_name/model/endpoint: 判断Provider設定, game_directory: sessionデータ保存先, dry_run: 実入力を禁止する設定, window_handle/window_process_id/input_mode: 入力対象と入力方式]
# }
class SessionRuntimeConfiguration:
    provider_name: str
    model: str
    endpoint: str
    game_directory: Path
    dry_run: bool
    window_handle: int | None
    input_mode: str
    window_process_id: int | None


# {
#   責務: [ManagedSessionRuntime: GameSessionControllerが1Session中に共有するpipelineとproviderの寿命を管理する]
#   フィールド: [pipeline: 判断・実行のsession runtime, _provider: pipelineとloop評価が共有するprovider]
# }
class ManagedSessionRuntime:
    # {
    #   責務: [__init__: Session Controllerが所有するPipelineとProviderを関連付ける]
    #   処理: [shutdown成功状態を個別に初期化し、失敗したresourceだけを再試行可能にする]
    #   引数: [pipeline: session中に再利用する判断・実行pipeline, provider: pipelineへ注入したsession scoped provider]
    #   戻り値: []
    # }
    def __init__(self, pipeline: DecisionPipeline, provider: Any) -> None:
        self.pipeline = pipeline
        self._provider = provider
        self._pipeline_closed = False
        self._provider_closed = False

    # {
    #   責務: [run: session pipelineの判断処理を実行する]
    #   処理: [引数を同じsession pipelineへ渡す]
    #   引数: [arguments: DecisionPipeline.runが受け取る実行設定]
    #   戻り値: [ActionDecision: session providerが選んだ操作]
    # }
    def run(self, **arguments: Any) -> ActionDecision:
        return self.pipeline.run(**arguments)

    # {
    #   責務: [run_and_execute: session pipelineで判断と安全検証付き実行を行う]
    #   処理: [引数を同じsession pipelineへ渡し、pipelineが管理する結果を返す]
    #   引数: [arguments: DecisionPipeline.run_and_executeが受け取る実行設定]
    #   戻り値: [ExecutionResult: 実行された候補と実行結果]
    # }
    def run_and_execute(self, **arguments: Any) -> ExecutionResult:
        return self.pipeline.run_and_execute(**arguments)

    # {
    #   責務: [load_execution_history: 同一Session Pipelineが保有する実行履歴を返す]
    #   引数: [なし]
    #   戻り値: [list[ExecutionResult]: 旧保存分と現在Journalへ追加した実行結果]
    # }
    def load_execution_history(self) -> list[ExecutionResult]:
        return self.pipeline.load_execution_history()

    # {
    #   責務: [assess_outcome: session Providerを再利用して現在画面のterminal状態を評価する]
    #   処理: [Pipeline判断に使うProviderへ現在と直前のObservationを渡す]
    #   引数: [observation: 現在画面, previous: session内の直前画面]
    #   戻り値: [OutcomeAssessment: success/failure/ongoing状態と根拠]
    # }
    def assess_outcome(
        self,
        observation: ScreenObservation,
        previous: ScreenObservation | None,
    ) -> OutcomeAssessment:
        assess = getattr(self._provider, "assess_outcome", None)
        if assess is None:
            raise RuntimeError("Session provider does not support outcome assessment")
        return assess(observation, previous)

    # {
    #   責務: [close: session終了時にPipelineとProviderのresourceを解放する]
    #   処理: [Pipelineを先に閉じ、失敗したresourceの状態だけを残して再試行可能にする]
    #   引数: []
    #   戻り値: []
    #   エラー: [RuntimeError: いずれかのresource解放に失敗した場合]
    # }
    def close(self) -> None:
        errors: list[Exception] = []
        if not self._pipeline_closed:
            try:
                self.pipeline.close()
                self._pipeline_closed = True
            except Exception as exc:
                errors.append(exc)
        if not self._provider_closed:
            try:
                _close_provider(self._provider)
                self._provider_closed = True
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("Session runtime cleanup failed: " + "; ".join(map(str, errors))) from errors[0]


# {
#   責務: [RuntimeComposition: GUI設定をProvider・Evaluator・Pipeline実装へ組み立てるcomposition root]
#   フィールド: [_provider_factories: provider名ごとの生成経路, _pipeline_factory: session pipeline生成経路, action_evaluator/outcome_evaluator: application共有評価器]
# }
class RuntimeComposition:
    # {
    #   責務: [__init__: Provider・Pipeline・Evaluatorのcomposition pointを構成する]
    #   処理: [標準factoryまたはテストから注入されたfactoryを保持し、評価器を一度だけ生成する]
    #   引数: [provider_factories: Provider名別の生成関数, pipeline_factory: Pipeline生成関数, action_evaluator_factory/outcome_evaluator_factory: 評価器生成関数]
    #   戻り値: []
    # }
    def __init__(
        self,
        *,
        provider_factories: Mapping[str, Callable[[str, str], Any]] | None = None,
        pipeline_factory: Callable[..., DecisionPipeline] = DecisionPipeline,
        action_evaluator_factory: Callable[[], ActionEvaluator] = ActionEvaluator,
        outcome_evaluator_factory: Callable[[], OutcomeEvaluator] = OutcomeEvaluator,
    ) -> None:
        self._provider_factories = dict(
            provider_factories
            if provider_factories is not None
            else {
                "Ollama": lambda model, endpoint: OllamaProvider(model, endpoint),
                "ローカル規則": lambda _model, _endpoint: RuleProvider(),
            }
        )
        self._pipeline_factory = pipeline_factory
        self.action_evaluator = action_evaluator_factory()
        self.outcome_evaluator = outcome_evaluator_factory()

    # {
    #   責務: [create_session_runtime: 1 Session用のProviderとPipelineを生成して所有関係を返す]
    #   処理: [session設定からProviderと専用Journalを生成し、未移行時だけ旧記録を取り込んでから完了markerを作る]
    #   引数: [configuration: Session開始時の確定設定, source: session中に更新されるObservation source, controller: 停止世代を共有するcontroller, automated_cursor_position_callback: 実入力カーソル位置の通知先, runtime_log: active Sessionへruntime eventを記録するapplication logger]
    #   戻り値: [ManagedSessionRuntime: closeでPipelineとProviderを順序付き解放するsession runtime]
    #   エラー: [ValueError: Provider factoryが未登録の場合, Exception: Pipeline初期化失敗]
    # }
    def create_session_runtime(
        self,
        configuration: SessionRuntimeConfiguration,
        *,
        source: ObservationSource,
        controller: RunController,
        automated_cursor_position_callback: Callable[[tuple[int, int] | None, bool], None] | None = None,
        runtime_log: RuntimeLog | None = None,
    ) -> ManagedSessionRuntime:
        provider_factory = self._provider_factories.get(configuration.provider_name)
        if provider_factory is None:
            raise ValueError(f"Unsupported decision provider: {configuration.provider_name}")

        provider = provider_factory(configuration.model, configuration.endpoint)
        session_id = uuid4().hex
        migration_marker = configuration.game_directory / "session_events" / "legacy_migration_v1.complete"
        migrate_legacy = not migration_marker.exists()
        event_journal: EventJournal | None = None
        pipeline: DecisionPipeline | None = None
        try:
            event_journal = EventJournal(
                configuration.game_directory / "session_events" / f"{session_id}.sqlite3",
                session_id=session_id,
            )
            pipeline = self._pipeline_factory(
                source,
                configuration.game_directory,
                provider,
                controller,
                dry_run=configuration.dry_run,
                window_handle=configuration.window_handle,
                input_mode=configuration.input_mode,
                window_process_id=configuration.window_process_id,
                automated_cursor_position_callback=automated_cursor_position_callback,
                event_journal=event_journal,
                runtime_log=runtime_log,
                migrate_legacy=migrate_legacy,
            )
            if migrate_legacy:
                migration_marker.parent.mkdir(parents=True, exist_ok=True)
                migration_marker.touch(exist_ok=True)
        except Exception as initialization_error:
            cleanup_errors: list[Exception] = []
            if pipeline is not None:
                try:
                    pipeline.close()
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if event_journal is not None:
                try:
                    event_journal.close()
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            try:
                _close_provider(provider)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise RuntimeError(
                    f"Session pipeline initialization failed ({initialization_error}); "
                    "cleanup also failed (" + "; ".join(map(str, cleanup_errors)) + ")"
                ) from initialization_error
            raise
        return ManagedSessionRuntime(pipeline, provider)

    # {
    #   責務: [assess_outcome: 指定Providerまたはローカルfallbackで画面状態を評価する]
    #   処理: [Ollama providerは評価後に解放し、provider以外は共有OutcomeEvaluatorを使う]
    #   引数: [provider_name/model/endpoint: 評価Provider設定, observation: 現在画面, previous: 直前画面]
    #   戻り値: [OutcomeAssessment: success/failure/ongoing状態と根拠]
    #   エラー: [Provider接続・評価に失敗した場合は呼び出し元へ送出]
    # }
    def assess_outcome(
        self,
        provider_name: str,
        model: str,
        endpoint: str,
        observation: ScreenObservation,
        previous: ScreenObservation | None,
    ) -> OutcomeAssessment:
        if provider_name != "Ollama":
            return self.outcome_evaluator.assess(observation)
        provider_factory = self._provider_factories.get(provider_name)
        if provider_factory is None:
            raise ValueError(f"Unsupported outcome provider: {provider_name}")
        provider = provider_factory(model, endpoint)
        try:
            return provider.assess_outcome(observation, previous)
        finally:
            _close_provider(provider)

    # {
    #   責務: [assess_locally: 共有OutcomeEvaluatorで画面状態を評価する]
    #   処理: [OCR textのterminal keywordをローカル規則で調べる]
    #   引数: [observation: 評価対象の画面観測]
    #   戻り値: [OutcomeAssessment: success/failure/ongoing状態と根拠]
    # }
    def assess_locally(self, observation: ScreenObservation) -> OutcomeAssessment:
        return self.outcome_evaluator.assess(observation)

    # {
    #   責務: [explain_actions: 共有ActionEvaluatorで候補の許可・拒否理由を返す]
    #   処理: [同じevaluator instanceへ現在の画面と候補を渡す]
    #   引数: [observation: 候補座標の検証に使う画面, candidates: 検査する操作候補]
    #   戻り値: [list[dict[str, object]]: 候補ごとのaccepted状態と理由]
    # }
    def explain_actions(
        self,
        observation: ScreenObservation,
        candidates: list[ActionCandidate],
    ) -> list[dict[str, object]]:
        return self.action_evaluator.explain(observation, candidates)

    # {
    #   責務: [list_models: 指定Ollama endpointから利用可能なモデル名を取得する]
    #   処理: [OllamaProviderのmodel catalog APIへendpointを渡す]
    #   引数: [endpoint: Ollama HTTP endpoint]
    #   戻り値: [list[str]: endpointが返したmodel名]
    #   エラー: [RuntimeError: endpointへ接続できないか応答形式を解釈できない場合]
    # }
    @staticmethod
    def list_models(endpoint: str) -> list[str]:
        return OllamaProvider.list_models(endpoint)

# {
#   責務: [_close_provider: Providerが公開するshutdown処理を実行する]
#   処理: [closeを優先し、未定義の場合だけshutdownを呼び出す]
#   引数: [provider: 解放するsession provider]
#   戻り値: []
# }
def _close_provider(provider: Any) -> None:
    close = getattr(provider, "close", None)
    if close is None:
        close = getattr(provider, "shutdown", None)
    if close is not None:
        close()
