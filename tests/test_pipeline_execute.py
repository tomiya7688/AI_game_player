import tempfile
import queue
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_game_player.app import Application
from ai_game_player.models import ActionCandidate, ActionDecision, ScreenObservation
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.provider import RuleProvider
from ai_game_player.action_executor import ExecutionResult
from ai_game_player.engine import GamePlayerEngine
from ai_game_player.run_control import ExecutionCancelled, RunController


# {
#   責務: [PipelineExecuteTest timeouts: 非同期停止回帰の待機上限を名前付きで示す]
#   フィールド: [provider_response_wait_seconds: provider待機上限, provider_start_wait_seconds: 起動確認上限, background_result_wait_seconds: worker結果待機上限]
# }
PROVIDER_RESPONSE_WAIT_SECONDS = 5
PROVIDER_START_WAIT_SECONDS = 2
BACKGROUND_RESULT_WAIT_SECONDS = 5


class Source:
    def read(self):
        return ScreenObservation("menu", 100, 80, ["OCR START"]), [ActionCandidate("configured", "wait", "wait")]


class PipelineExecuteTest(unittest.TestCase):
    def test_pipeline_passes_automated_cursor_notification_to_executor(self):
        def callback(_position, _move_in_progress):
            return None

        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(
                Source(),
                Path(directory),
                automated_cursor_position_callback=callback,
            )

        self.assertIs(pipeline.executor.automated_cursor_position_callback, callback)

    def test_stop_invalidates_executor_for_original_run_after_rearm(self):
        controller = RunController()
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(Source(), Path(directory), controller=controller)
            original_run_token = controller.rearm_token
            pipeline._bind_execution_stop_checker(original_run_token)

            self.assertFalse(pipeline.executor._stop_requested())
            controller.stop("Stop button")
            self.assertTrue(pipeline.executor._stop_requested())
            controller.start()
            self.assertTrue(pipeline.executor._stop_requested())
            pipeline._bind_execution_stop_checker(controller.rearm_token)
            self.assertFalse(pipeline.executor._stop_requested())

    def test_run_and_execute_defaults_to_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            result = DecisionPipeline(Source(), Path(directory)).run_and_execute()
            self.assertFalse(result.executed)
            self.assertEqual(result.mode, "dry_run")

    def test_run_and_execute_can_execute_ocr_merged_candidate(self):
        class Provider(RuleProvider):
            def choose(self, candidates, observation, purpose="", personality=""):
                return ActionDecision("ocr-0", "ocr", "test")
        with tempfile.TemporaryDirectory() as directory:
            result = DecisionPipeline(Source(), Path(directory), Provider()).run_and_execute([{"text": "OCR START", "x": 10, "y": 10, "width": 20, "height": 10}])
            self.assertEqual(result.action_id, "ocr-0")

    def test_run_and_execute_reads_source_once_and_uses_same_candidates(self):
        class ChangingSource:
            def __init__(self):
                self.read_count = 0

            def read(self):
                self.read_count += 1
                if self.read_count == 1:
                    return ScreenObservation("first", 100, 80, []), [ActionCandidate("first-action", "wait", "wait")]
                return ScreenObservation("second", 100, 80, []), [ActionCandidate("second-action", "wait", "wait")]

        class Provider(RuleProvider):
            def choose(self, candidates, observation, purpose="", personality=""):
                return ActionDecision(candidates[0].action_id, "snapshot", "test")

        source = ChangingSource()
        with tempfile.TemporaryDirectory() as directory:
            result = DecisionPipeline(source, Path(directory), Provider()).run_and_execute()

        self.assertEqual(source.read_count, 1)
        self.assertEqual(result.action_id, "first-action")

    def test_run_and_execute_passes_the_selected_candidate_instance_to_execution(self):
        selected_candidate = ActionCandidate("selected", "wait", "Wait")
        executed_candidates = []

        class SingleCandidateSource:
            def read(self):
                return ScreenObservation("menu", 100, 80, []), [selected_candidate]

        class Provider(RuleProvider):
            def choose(self, candidates, observation, purpose="", personality=""):
                return ActionDecision(candidates[0].action_id, "select exact candidate", "test")

        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(SingleCandidateSource(), Path(directory), Provider())

            def record_execution(candidate):
                executed_candidates.append(candidate)
                return ExecutionResult(candidate.action_id, False, "test", "recorded")

            pipeline.executor.execute = record_execution
            pipeline.run_and_execute()

        self.assertEqual(len(executed_candidates), 1)
        self.assertIs(executed_candidates[0], selected_candidate)

    def test_run_and_execute_never_executes_a_candidate_from_an_ambiguous_id(self):
        class DuplicateIdSource:
            def read(self):
                return ScreenObservation("menu", 100, 80, []), [
                    ActionCandidate("duplicate", "click", "Outside", 200, 10, .9),
                    ActionCandidate("duplicate", "wait", "Wait", confidence=.9),
                ]

        executed_candidates = []
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(DuplicateIdSource(), Path(directory))

            def record_execution(candidate):
                executed_candidates.append(candidate)
                return ExecutionResult(candidate.action_id, False, "test", "recorded")

            pipeline.executor.execute = record_execution
            with self.assertRaises(ValueError):
                pipeline.run_and_execute()

        self.assertEqual(executed_candidates, [])

    def test_run_and_execute_uses_original_candidate_after_provider_replaces_allowed_entry(self):
        original_candidate = ActionCandidate("same-id", "wait", "Original")
        replacement_candidate = ActionCandidate("same-id", "click", "Replacement", 90, 70, .9)
        executed_candidates = []

        class ReplacingProvider(RuleProvider):
            def choose(self, candidates, observation, purpose="", personality=""):
                candidates[0] = replacement_candidate
                return ActionDecision("same-id", "select same ID", "test")

        class OriginalSource:
            def read(self):
                return ScreenObservation("menu", 100, 80, []), [original_candidate]

        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(OriginalSource(), Path(directory), ReplacingProvider())
            pipeline.executor.execute = lambda candidate: executed_candidates.append(candidate) or ExecutionResult(candidate.action_id, False, "test", "recorded")

            pipeline.run_and_execute()

        self.assertEqual(len(executed_candidates), 1)
        self.assertIs(executed_candidates[0], original_candidate)

    def test_run_and_execute_dispatches_through_overridden_engine_step(self):
        alternate_candidate = ActionCandidate("alternate", "wait", "Alternate")
        executed_candidates = []

        class AlternateEngine(GamePlayerEngine):
            def step(self, observation, candidates, purpose="", personality=""):
                return ActionDecision(alternate_candidate.action_id, "extension choice", "test")

        class TwoCandidateSource:
            def read(self):
                return ScreenObservation("menu", 100, 80, []), [
                    ActionCandidate("default", "wait", "Default"),
                    alternate_candidate,
                ]

        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(TwoCandidateSource(), Path(directory))
            pipeline.engine = AlternateEngine(Path(directory))
            pipeline.executor.execute = lambda candidate: executed_candidates.append(candidate) or ExecutionResult(candidate.action_id, False, "test", "recorded")

            pipeline.run_and_execute()

        self.assertEqual(len(executed_candidates), 1)
        self.assertIs(executed_candidates[0], alternate_candidate)

    # {
    #   責務: [test_stop_during_provider_wait_discards_result_before_live_input: 推論待ち中の停止を受理し旧結果を実入力へ進ませない]
    #   処理: [遅延providerをworkerで待機させ、停止・再開後の戻り値がキャンセルされexecutor未呼出しであることを検証する]
    #   引数: []
    #   戻り値: []
    # }
    def test_stop_during_provider_wait_discards_result_before_live_input(self):
        provider_started = threading.Event()
        allow_provider_to_return = threading.Event()
        executed_actions = []

        # {
        #   責務: [DelayedProvider: Ollama相当の判断応答をテスト中に保留する]
        #   処理: [provider entryと解放待ちを外部eventへ公開する]
        # }
        class DelayedProvider(RuleProvider):
            # {
            #   責務: [choose: 停止テスト中にprovider応答を保留する]
            #   処理: [開始を通知して解放eventまで待ち、最初の許可候補を返す]
            #   引数: [candidates: 判断可能候補]
            #   戻り値: [ActionDecision: テスト用の遅延判断]
            # }
            def choose(self, candidates, observation=None, purpose="", personality=""):
                provider_started.set()
                if not allow_provider_to_return.wait(timeout=PROVIDER_RESPONSE_WAIT_SECONDS):
                    raise TimeoutError("test provider was not released")
                return ActionDecision(candidates[0].action_id, "delayed", "test")

        with tempfile.TemporaryDirectory() as directory:
            controller = RunController()
            pipeline = DecisionPipeline(Source(), Path(directory), DelayedProvider(), controller, dry_run=False)
            pipeline.executor.execute = lambda candidate: executed_actions.append(candidate.action_id)

            application = Application.__new__(Application)
            application.controller = controller
            application._execution_in_progress = False
            application._background_task_counter = 0
            application._execution_task_id = None
            application._background_results = queue.Queue()
            application.provider = SimpleNamespace(get=lambda: "Ollama")
            application._set_status = lambda _message: None

            run_token = controller.rearm_token
            self.assertTrue(application._start_pipeline_worker(pipeline, "execute", "", "", run_token, False))
            self.assertTrue(provider_started.wait(timeout=PROVIDER_START_WAIT_SECONDS))

            controller.stop("停止ボタン")
            controller.start()
            allow_provider_to_return.set()

            task_type, task_id, result_token, operation, _result, error, _loop_step, _observation, execution_records = application._background_results.get(timeout=BACKGROUND_RESULT_WAIT_SECONDS)

            logged_events = []
            application._execution_task_id = task_id
            application.runtime_log = SimpleNamespace(write=lambda *record: logged_events.append(record))
            application.root = SimpleNamespace(after=lambda *_args: None)
            statuses = []
            application._set_status = statuses.append
            application._background_results.put((task_type, task_id, result_token, operation, _result, error, _loop_step, _observation, execution_records))
            application._poll_background_results()

        self.assertEqual(task_type, "pipeline")
        self.assertEqual(result_token, run_token)
        self.assertEqual(operation, "execute")
        self.assertIsInstance(error, ExecutionCancelled)
        self.assertEqual(executed_actions, [])
        self.assertFalse(application._execution_in_progress)
        self.assertIn("停止理由: 停止ボタン", str(logged_events[0]))
        self.assertIn("停止要求後の推論応答を破棄しました", statuses)

    def test_stop_during_history_staging_discards_uncommitted_decision(self):
        history_staged = threading.Event()
        allow_staging_to_finish = threading.Event()
        worker_errors = []

        with tempfile.TemporaryDirectory() as directory:
            controller = RunController()
            pipeline = DecisionPipeline(Source(), Path(directory), controller=controller)
            original_prepare_append = pipeline.engine.history.prepare_append

            def delayed_history_staging(observation, decision):
                staged_write = original_prepare_append(observation, decision)
                history_staged.set()
                if not allow_staging_to_finish.wait(timeout=PROVIDER_RESPONSE_WAIT_SECONDS):
                    raise TimeoutError("test did not release staged history")
                return staged_write

            pipeline.engine.history.prepare_append = delayed_history_staging
            run_token = controller.rearm_token

            def run_decision():
                try:
                    pipeline.run(expected_rearm_token=run_token)
                except Exception as exc:
                    worker_errors.append(exc)

            worker = threading.Thread(target=run_decision, daemon=True)
            worker.start()
            self.assertTrue(history_staged.wait(timeout=PROVIDER_START_WAIT_SECONDS))

            controller.stop("停止中の履歴準備")
            self.assertFalse(controller.is_running)
            allow_staging_to_finish.set()
            worker.join(timeout=BACKGROUND_RESULT_WAIT_SECONDS)
            pipeline.close()

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(worker_errors), 1)
            self.assertIsInstance(worker_errors[0], ExecutionCancelled)
            self.assertFalse(pipeline.engine.history.path.exists())
            self.assertFalse(pipeline.engine.trace.path.exists())
            self.assertIsNone(pipeline.engine._previous_observation)
            self.assertIsNone(pipeline.engine._previous_action_id)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
