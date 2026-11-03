import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_game_player.decision_withheld import DecisionWithheld
from ai_game_player.hook_envelope import derive_hook_envelope
from ai_game_player.hook_registration import (
    HookFailurePolicy,
    HookMode,
    HookRegistration,
)
from ai_game_player.hook_runtime import HookRuntime
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.perception_hook_boundary import (
    PERCEPTION_HOOK_MAX_CANDIDATES,
    PERCEPTION_HOOK_PAYLOAD_SCHEMA,
    PerceptionHookBoundary,
)
from ai_game_player.pipeline import DecisionPipeline
from ai_game_player.planning_outcome import PlanningOutcome


def perception_hook(hook_id, mode, callback, **overrides):
    return HookRegistration(
        hook_id=hook_id,
        hook_version="1",
        mode=mode,
        stage="perception",
        boundary="output",
        callback=callback,
        timeout_ns=500_000_000,
        **overrides,
    )


def counter():
    value = iter(range(1, 1_000_000))
    return lambda: f"{next(value):04d}"


class Source:
    def read(self):
        return ScreenObservation("menu", 200, 100), [
            ActionCandidate("configured", "click", "Configured", 10, 10, 0.8)
        ]


class PerceptionHookBoundaryTest(unittest.TestCase):
    def make_boundary(self, runtime):
        return PerceptionHookBoundary(
            runtime,
            session_id="session-1",
            utc_now=lambda: datetime(2026, 11, 4, tzinfo=timezone.utc),
            monotonic_ns=lambda: 42,
            new_id=counter(),
        )

    def test_builds_coordinate_free_envelope_for_read_hooks(self):
        seen = []
        runtime = HookRuntime(enabled=True)
        boundary = self.make_boundary(runtime)
        runtime.register(perception_hook("demo.reader", HookMode.READ, seen.append))
        candidates = [
            ActionCandidate(f"a{i}", "click", "L" * 300, 10, 20, 0.5, bbox=(1, 2, 3, 4))
            for i in range(PERCEPTION_HOOK_MAX_CANDIDATES + 1)
        ]

        result = boundary.run(ScreenObservation("menu", 200, 100), candidates)

        envelope = seen[0]
        self.assertIs(result.envelope, envelope)
        self.assertEqual((envelope.stage, envelope.boundary), ("perception", "output"))
        self.assertEqual(envelope.producer_kind, "core")
        self.assertEqual(envelope.payload_schema, PERCEPTION_HOOK_PAYLOAD_SCHEMA)
        self.assertEqual(envelope.timestamp_utc, "2026-11-04T00:00:00.000000Z")
        self.assertEqual(envelope.monotonic_ns, 42)
        payload = envelope.payload
        self.assertEqual(payload["candidate_count"], PERCEPTION_HOOK_MAX_CANDIDATES + 1)
        self.assertEqual(len(payload["candidates"]), PERCEPTION_HOOK_MAX_CANDIDATES)
        self.assertEqual(len(payload["candidates"][0]["label"]), 256)
        for item in payload["candidates"]:
            self.assertFalse({"x", "y", "bbox", "hold_seconds"} & set(item))
        self.assertIs(boundary.last_result, result)

    def test_rejects_modify_hooks_at_this_boundary(self):
        runtime = HookRuntime(enabled=True)
        self.make_boundary(runtime)
        with self.assertRaisesRegex(ValueError, "not allowed"):
            runtime.register(
                perception_hook("demo.modify", HookMode.MODIFY, lambda e: e)
            )

    def test_skips_envelope_work_when_disabled_or_empty(self):
        calls = []
        runtime = HookRuntime()
        boundary = self.make_boundary(runtime)
        runtime.register(perception_hook("demo.reader", HookMode.READ, calls.append))
        self.assertIsNone(boundary.run(ScreenObservation("menu", 2, 2), []))
        runtime.enabled = True
        runtime.unregister("demo.reader")
        self.assertIsNone(boundary.run(ScreenObservation("menu", 2, 2), []))
        self.assertEqual(calls, [])

    def test_halting_hook_failure_withholds_the_decision(self):
        runtime = HookRuntime(enabled=True)
        boundary = self.make_boundary(runtime)

        def fail(_):
            raise RuntimeError("boom")

        runtime.register(
            perception_hook(
                "demo.required", HookMode.READ, fail, on_failure=HookFailurePolicy.HALT
            )
        )
        with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
            with self.assertRaises(DecisionWithheld) as caught:
                boundary.run(ScreenObservation("menu", 2, 2), [])
        disposition = caught.exception.disposition
        self.assertEqual(disposition.outcome, PlanningOutcome.ABSTAIN)
        self.assertEqual(disposition.reason_codes, ("hook_halted",))
        self.assertTrue(boundary.last_result.halted)

    def test_unbuildable_envelope_fails_closed_only_for_halting_hooks(self):
        runtime = HookRuntime(enabled=True)
        boundary = self.make_boundary(runtime)
        runtime.register(perception_hook("demo.reader", HookMode.READ, lambda _: None))
        bad = ScreenObservation("\ud800", 2, 2)
        with self.assertLogs("ai_game_player.perception_hook_boundary", "WARNING"):
            self.assertIsNone(boundary.run(bad, []))
        runtime.register(
            perception_hook(
                "demo.required",
                HookMode.READ,
                lambda _: None,
                on_failure=HookFailurePolicy.HALT,
            )
        )
        with self.assertRaises(DecisionWithheld) as caught:
            boundary.run(bad, [])
        self.assertEqual(
            caught.exception.disposition.reason_codes, ("hook_envelope_unavailable",)
        )


class PipelinePerceptionHookTest(unittest.TestCase):
    def test_pipeline_without_runtime_has_no_hook_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(Source(), Path(directory))
            self.assertIsNone(pipeline.perception_hooks)
            self.assertEqual(pipeline.run().action_id, "configured")

    def test_hooks_observe_and_inject_without_changing_the_decision(self):
        seen = []
        runtime = HookRuntime(enabled=True)
        holder = {}

        def inject(envelope):
            return [
                derive_hook_envelope(
                    envelope,
                    holder["demo.inject"],
                    stage="ext.demo.annotation",
                    payload_schema="demo.annotation",
                    payload={"note": "seen"},
                )
            ]

        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(Source(), Path(directory), hook_runtime=runtime)
            runtime.register(perception_hook("demo.reader", HookMode.READ, seen.append))
            holder["demo.inject"] = perception_hook(
                "demo.inject", HookMode.INJECT, inject
            )
            runtime.register(holder["demo.inject"])

            decision = pipeline.run()

            self.assertEqual(decision.action_id, "configured")
            self.assertEqual(
                [item["action_id"] for item in seen[0].payload["candidates"]],
                ["configured"],
            )
            result = pipeline.perception_hooks.last_result
            self.assertEqual(len(result.injected), 1)
            self.assertEqual(result.injected[0].stage, "ext.demo.annotation")

    def test_halting_hook_failure_blocks_execution_before_any_input(self):
        runtime = HookRuntime(enabled=True)

        def fail(_):
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as directory:
            pipeline = DecisionPipeline(Source(), Path(directory), hook_runtime=runtime)
            runtime.register(
                perception_hook(
                    "demo.required",
                    HookMode.READ,
                    fail,
                    on_failure=HookFailurePolicy.HALT,
                )
            )
            with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
                with self.assertRaises(DecisionWithheld):
                    pipeline.run_and_execute()
            self.assertFalse((Path(directory) / "execution_history.json").exists())
            self.assertIsNone(pipeline.engine.last_decision_context)


if __name__ == "__main__":
    unittest.main()
