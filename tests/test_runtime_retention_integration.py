import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from ai_game_player.artifact_admission import ArtifactAdmissionRequest
from ai_game_player.event_journal import EventJournal
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.retention_collector import RetentionCollector
from ai_game_player.retention_policy import RetentionPolicy
from ai_game_player.run_control import RunController
from ai_game_player.runtime.retention_adapter import (
    RetentionTriggerPolicy,
    RuntimeRetentionAdapter,
    RuntimeRetentionReport,
)
from ai_game_player.runtime.session_composition import (
    RuntimeComposition,
    SessionRetentionConfig,
    SessionRuntimeConfiguration,
)

MEDIA_TYPE = "application/octet-stream"


class Provider:
    def close(self) -> None:
        return None


def configuration(
    game_directory: Path, retention: SessionRetentionConfig | None = None
) -> SessionRuntimeConfiguration:
    values = dict(
        provider_name="fake",
        model="model",
        endpoint="http://localhost",
        game_directory=game_directory,
        dry_run=True,
        window_handle=None,
        input_mode="window_message",
        window_process_id=None,
    )
    if retention is not None:
        values["retention"] = retention
    return SessionRuntimeConfiguration(**values)


class RetentionIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.game_directory = Path(self.temp.name) / "game"
        self.captured: dict = {}

    def composition(self) -> RuntimeComposition:
        captured = self.captured

        def factory(
            source,
            game_directory,
            provider,
            controller,
            *,
            event_journal,
            migrate_legacy,
            migrate_runtime_log,
            retention_adapter=None,
        ):
            captured["adapter"] = retention_adapter
            return DecisionPipeline(
                source,
                game_directory,
                provider,
                controller,
                event_journal=event_journal,
                migrate_legacy=migrate_legacy,
                migrate_runtime_log=migrate_runtime_log,
            )

        return RuntimeComposition(
            provider_factories={"fake": lambda *_arguments: Provider()},
            pipeline_factory=factory,
        )

    def create(self, retention: SessionRetentionConfig | None = None):
        return self.composition().create_session_runtime(
            configuration(self.game_directory, retention),
            source=object(),
            controller=RunController(),
        )

    def request(self, payload: bytes, entry_id: str) -> ArtifactAdmissionRequest:
        return ArtifactAdmissionRequest(
            payload_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            artifact_type="frame.raw",
            media_type=MEDIA_TYPE,
            entry_id=entry_id,
            protected=False,
            source_ref="integration.test",
        )

    def test_t8_close_runs_collect_with_verified_live_references_and_traces(
        self,
    ) -> None:
        policy = RetentionPolicy(10**6, 2 * 10**6, default_ttl_ms=1)
        runtime = self.create(SessionRetentionConfig(policy=policy))
        adapter = self.captured["adapter"]
        self.assertIsInstance(adapter, RuntimeRetentionAdapter)
        self.assertIs(adapter, runtime.retention)
        journal = runtime.pipeline.event_journal

        kept_payload, gone_payload = b"kept-payload", b"gone-payload"
        kept = adapter.persist(
            self.request(kept_payload, "entry.kept"),
            lambda port: port.put(kept_payload),
        )
        gone = adapter.persist(
            self.request(gone_payload, "entry.gone"),
            lambda port: port.put(gone_payload),
        )
        with adapter.recording([kept.reference]):
            journal.append("artifact.recorded", artifact_refs=(kept.reference,))
        adapter.release(gone.token)
        time.sleep(0.01)

        with mock.patch.object(
            RetentionCollector,
            "collect",
            autospec=True,
            side_effect=RetentionCollector.collect,
        ) as collect:
            runtime.close()

        self.assertEqual(1, collect.call_count)
        (_collector, live), kwargs = collect.call_args
        self.assertEqual({}, kwargs)
        self.assertEqual({kept.reference.sha256}, {item.sha256 for item in live})
        result = runtime.retention_close_result
        self.assertIsInstance(result, RuntimeRetentionReport)
        root = self.game_directory / "session_artifacts"
        (session_root,) = list(root.iterdir())
        stored = {path.name for path in session_root.glob("??/*") if path.is_dir()}
        self.assertEqual({kept.reference.sha256}, stored)

        (database,) = sorted((self.game_directory / "session_events").glob("*.sqlite3"))
        with EventJournal(database) as reopened:
            last = reopened.last_sequence
            batch = reopened.artifact_refs_after(0, limit=1000)
        self.assertEqual((kept.reference,), batch.refs)
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute(
                "SELECT envelope_json FROM events WHERE sequence=?", (last,)
            ).fetchall()
        envelope = json.loads(rows[0][0])
        self.assertEqual("retention.gc/v1", envelope["event_type"])
        self.assertEqual("session_close", envelope["payload"]["trigger"])
        self.assertEqual(1, envelope["payload"]["deleted_artifacts"])
        self.assertEqual([], envelope["artifact_refs"])

    def test_disabled_retention_creates_no_adapter_and_never_collects(self) -> None:
        with mock.patch.object(RetentionCollector, "collect") as collect:
            runtime = self.create(SessionRetentionConfig(enabled=False))
            self.assertIsNone(self.captured["adapter"])
            self.assertIsNone(runtime.retention)
            runtime.close()
        collect.assert_not_called()
        self.assertFalse((self.game_directory / "session_artifacts").exists())

    def test_session_without_artifacts_writes_nothing_and_adds_no_event(self) -> None:
        runtime = self.create()

        runtime.close()

        self.assertFalse((self.game_directory / "session_artifacts").exists())
        self.assertEqual("store_absent", runtime.retention_close_result.reason)
        (database,) = sorted((self.game_directory / "session_events").glob("*.sqlite3"))
        with closing(sqlite3.connect(database)) as connection:
            types = {
                json.loads(row[0])["event_type"]
                for row in connection.execute("SELECT envelope_json FROM events")
            }
        self.assertNotIn("retention.gc/v1", types)

    def test_invalid_retention_policy_fails_session_start_and_cleans_up(self) -> None:
        bad = SessionRetentionConfig(
            experience_paths=(Path("a/exp.jsonl"), Path("b/exp.jsonl"))
        )
        with self.assertRaises(ValueError):
            self.create(bad)
        self.assertEqual(
            [], list((self.game_directory / "session_events").glob("*.sqlite3"))
        )

    def test_factory_with_var_keyword_does_not_receive_the_adapter(self) -> None:
        received: dict = {}

        def factory(source, game_directory, provider, controller, **keywords):
            received.update(keywords)
            return DecisionPipeline(
                source,
                game_directory,
                provider,
                controller,
                event_journal=keywords["event_journal"],
                migrate_legacy=keywords["migrate_legacy"],
                migrate_runtime_log=keywords["migrate_runtime_log"],
            )

        runtime = RuntimeComposition(
            provider_factories={"fake": lambda *_arguments: Provider()},
            pipeline_factory=factory,
        ).create_session_runtime(
            configuration(self.game_directory),
            source=object(),
            controller=RunController(),
        )
        runtime.close()
        self.assertNotIn("retention_adapter", received)
        self.assertIsNotNone(runtime.retention)


class TriggerPolicyDefaultsTest(unittest.TestCase):
    def test_defaults_match_the_design(self) -> None:
        policy = RetentionTriggerPolicy()
        self.assertEqual(5_000, policy.min_interval_ms)
        self.assertEqual(64, policy.persist_count_threshold)
        self.assertEqual(300_000, policy.periodic_interval_ms)
        self.assertEqual(256, policy.max_deleted_artifacts_per_run)
        self.assertEqual(0.5, policy.trigger_lock_timeout_seconds)
        self.assertEqual(5.0, policy.close_lock_timeout_seconds)
        self.assertEqual(2_000, policy.snapshot_max_age_ms)
        self.assertEqual(10_000, policy.journal_batch_limit)


if __name__ == "__main__":
    unittest.main()
