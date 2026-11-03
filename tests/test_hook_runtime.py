import dataclasses
import threading
import unittest

from ai_game_player.experience import ArtifactReference
from ai_game_player.hook_dispatch_result import (
    HOOK_STATUS_COMPLETED,
    HOOK_STATUS_FAILED,
    HOOK_STATUS_QUARANTINED,
    HOOK_STATUS_TIMEOUT,
)
from ai_game_player.hook_envelope import derive_hook_envelope
from ai_game_player.hook_registration import (
    HookFailurePolicy,
    HookMode,
    HookRegistration,
)
from ai_game_player.hook_runtime import HookRuntime
from ai_game_player.stage_envelope import StageEnvelope

ALL_MODES = (HookMode.READ, HookMode.MODIFY, HookMode.INJECT)


def make_envelope(**overrides) -> StageEnvelope:
    values = {
        "envelope_id": "env-1",
        "stage": "state",
        "boundary": "output",
        "session_id": "session-1",
        "timestamp_utc": "2026-11-04T00:00:00Z",
        "trace_id": "trace-1",
        "producer_kind": "core",
        "producer_id": "core.state_builder",
        "producer_version": "1",
        "payload_schema": "kadoka.state.snapshot",
        "payload_schema_version": 1,
        "payload": {"score": 1, "labels": ["a"]},
    }
    values.update(overrides)
    return StageEnvelope(**values)


def registration(hook_id, mode, callback, **overrides) -> HookRegistration:
    values = {
        "hook_id": hook_id,
        "hook_version": "1.0",
        "mode": mode,
        "stage": "state",
        "boundary": "output",
        "callback": callback,
        "timeout_ns": 500_000_000,
    }
    values.update(overrides)
    return HookRegistration(**values)


def runtime_with_state_point(**kwargs) -> HookRuntime:
    kwargs.setdefault("enabled", True)
    runtime = HookRuntime(**kwargs)
    runtime.declare_point("state", "output", ALL_MODES)
    return runtime


class HookRegistrationTest(unittest.TestCase):
    def test_modify_defaults_to_halt_and_others_to_skip(self):
        def noop(_):
            return None

        self.assertIs(
            registration("a.modify", HookMode.MODIFY, noop).on_failure,
            HookFailurePolicy.HALT,
        )
        self.assertIs(
            registration("a.read", HookMode.READ, noop).on_failure,
            HookFailurePolicy.SKIP,
        )
        self.assertIs(
            registration("a.inject", HookMode.INJECT, noop).on_failure,
            HookFailurePolicy.SKIP,
        )

    def test_rejects_invalid_ids_and_unbounded_budgets(self):
        def noop(_):
            return None

        invalid = [
            {"hook_id": "Upper"},
            {"hook_version": ""},
            {"boundary": "middle"},
            {"timeout_ns": 0},
            {"timeout_ns": 1_000_000_001},
            {"max_output_bytes": 0},
            {"max_injected": 17},
            {"priority": True},
            {"callback": "not callable"},
        ]
        for override in invalid:
            with self.subTest(override=override):
                values = {
                    "hook_id": "a.hook",
                    "mode": HookMode.READ,
                    "callback": noop,
                    **override,
                }
                hook_id = values.pop("hook_id")
                mode = values.pop("mode")
                callback = values.pop("callback")
                with self.assertRaises(ValueError):
                    registration(hook_id, mode, callback, **values)


class HookRuntimeRegistryTest(unittest.TestCase):
    def test_disabled_by_default_and_never_calls_hooks(self):
        calls = []
        runtime = HookRuntime()
        runtime.declare_point("state", "output", ALL_MODES)
        runtime.register(registration("a.read", HookMode.READ, calls.append))
        envelope = make_envelope()

        result = runtime.dispatch(envelope)

        self.assertFalse(runtime.enabled)
        self.assertIs(result.envelope, envelope)
        self.assertEqual(result.records, ())
        self.assertFalse(runtime.has_hooks("state", "output"))
        self.assertEqual(calls, [])

    def test_registers_hooks_at_multiple_stages(self):
        runtime = HookRuntime(enabled=True)
        for stage in ("perception", "state", "reasoning", "evaluation", "ext.demo"):
            runtime.declare_point(stage, "input", ALL_MODES)
            runtime.register(
                registration(
                    f"demo.{stage.replace('.', '_')}",
                    HookMode.READ,
                    lambda _: None,
                    stage=stage,
                    boundary="input",
                )
            )
        for stage in ("perception", "state", "reasoning", "evaluation", "ext.demo"):
            self.assertTrue(runtime.has_hooks(stage, "input"))
            self.assertFalse(runtime.has_hooks(stage, "output"))

    def test_safety_and_execution_points_accept_read_only(self):
        runtime = HookRuntime(enabled=True)
        for stage in ("safety", "execution"):
            with self.subTest(stage=stage):
                with self.assertRaisesRegex(ValueError, "read hooks only"):
                    runtime.declare_point(stage, "input", ALL_MODES)
                runtime.declare_point(stage, "input", (HookMode.READ,))
                with self.assertRaisesRegex(ValueError, "not allowed"):
                    runtime.register(
                        registration(
                            f"x.{stage}",
                            HookMode.MODIFY,
                            lambda e: e,
                            stage=stage,
                            boundary="input",
                        )
                    )

    def test_rejects_undeclared_points_disallowed_modes_and_duplicates(self):
        runtime = HookRuntime(enabled=True)
        with self.assertRaisesRegex(ValueError, "not declared"):
            runtime.register(registration("a.read", HookMode.READ, lambda _: None))
        runtime.declare_point("state", "output", (HookMode.READ,))
        with self.assertRaisesRegex(ValueError, "not allowed"):
            runtime.register(registration("a.mod", HookMode.MODIFY, lambda e: e))
        runtime.register(registration("a.read", HookMode.READ, lambda _: None))
        with self.assertRaisesRegex(ValueError, "already registered"):
            runtime.register(registration("a.read", HookMode.READ, lambda _: None))
        with self.assertRaisesRegex(ValueError, "already declared"):
            runtime.declare_point("state", "output", ALL_MODES)
        with self.assertRaisesRegex(ValueError, "stage"):
            runtime.declare_point("unknown", "output", ALL_MODES)
        self.assertTrue(runtime.unregister("a.read"))
        self.assertFalse(runtime.unregister("a.read"))

    def test_authorizer_can_deny_registration(self):
        seen = []

        def deny_modify(item):
            seen.append(item.hook_id)
            if item.mode is HookMode.MODIFY:
                raise PermissionError("modify capability missing")

        runtime = runtime_with_state_point(authorizer=deny_modify)
        runtime.register(registration("a.read", HookMode.READ, lambda _: None))
        with self.assertRaises(PermissionError):
            runtime.register(registration("a.mod", HookMode.MODIFY, lambda e: e))
        self.assertEqual(seen, ["a.read", "a.mod"])
        self.assertTrue(runtime.has_hooks("state", "output"))
        self.assertFalse(runtime.has_halting_hooks("state", "output"))


class HookRuntimeDispatchTest(unittest.TestCase):
    def test_orders_modify_read_inject_deterministically(self):
        calls = []
        runtime = runtime_with_state_point()

        def modifier(name, score):
            def modify(envelope):
                calls.append(name)
                return derive_hook_envelope(
                    envelope,
                    holder[name],
                    payload={**envelope.payload, "score": score},
                    envelope_id=f"{name}-out",
                )

            return modify

        def reader(name):
            def read(envelope):
                calls.append((name, envelope.payload["score"]))

            return read

        def injector(envelope):
            calls.append("inject")
            return [
                derive_hook_envelope(
                    envelope,
                    holder["z.inject"],
                    stage="ext.demo.summary",
                    payload_schema="demo.summary",
                    payload={"score": envelope.payload["score"]},
                    envelope_id="inject-out",
                )
            ]

        holder = {}
        regs = [
            registration("z.inject", HookMode.INJECT, injector, priority=-5),
            registration("a.read", HookMode.READ, reader("a.read"), priority=1),
            registration("b.read", HookMode.READ, reader("b.read"), priority=1),
            registration("c.read", HookMode.READ, reader("c.read"), priority=0),
        ]
        modify_b = registration("b.modify", HookMode.MODIFY, modifier("b.modify", 3))
        modify_a = registration(
            "a.modify", HookMode.MODIFY, modifier("a.modify", 2), priority=0
        )
        holder = {item.hook_id: item for item in (*regs, modify_a, modify_b)}
        for item in (*regs, modify_b, modify_a):
            runtime.register(item)

        result = runtime.dispatch(make_envelope())

        self.assertEqual(
            calls,
            [
                "a.modify",
                "b.modify",
                ("c.read", 3),
                ("a.read", 3),
                ("b.read", 3),
                "inject",
            ],
        )
        self.assertEqual(result.envelope.envelope_id, "b.modify-out")
        self.assertEqual(result.envelope.parent_envelope_id, "a.modify-out")
        self.assertEqual(result.envelope.payload["score"], 3)
        self.assertEqual([item.envelope_id for item in result.injected], ["inject-out"])
        self.assertEqual(result.injected[0].parent_envelope_id, "b.modify-out")
        self.assertFalse(result.halted)
        self.assertTrue(
            all(record.status == HOOK_STATUS_COMPLETED for record in result.records)
        )

    def test_modify_output_is_a_fresh_revalidated_envelope(self):
        runtime = runtime_with_state_point()
        holder = {}

        def modify(envelope):
            return derive_hook_envelope(
                envelope, holder["a.mod"], payload={"score": 9}, envelope_id="out-1"
            )

        holder["a.mod"] = registration("a.mod", HookMode.MODIFY, modify)
        runtime.register(holder["a.mod"])
        source = make_envelope()

        result = runtime.dispatch(source)

        self.assertEqual(result.envelope.producer_kind, "hook")
        self.assertEqual(result.envelope.producer_id, "a.mod")
        self.assertEqual(result.envelope.producer_version, "1.0")
        self.assertEqual(result.envelope.derived_from, ("env-1",))
        self.assertEqual(source.payload["score"], 1)
        self.assertEqual(result.records[0].output_envelope_ids, ("out-1",))

    def test_read_hooks_cannot_mutate_or_return_values(self):
        runtime = runtime_with_state_point()

        def mutate(envelope):
            envelope.payload["score"] = 99

        runtime.register(registration("a.mutate", HookMode.READ, mutate))
        runtime.register(registration("b.returns", HookMode.READ, lambda e: e))
        source = make_envelope()

        with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
            result = runtime.dispatch(source)

        self.assertIs(result.envelope, source)
        self.assertEqual(source.payload["score"], 1)
        self.assertEqual(
            [(r.hook_id, r.status, r.failure_kind) for r in result.records],
            [
                ("a.mutate", HOOK_STATUS_FAILED, "exception"),
                ("b.returns", HOOK_STATUS_FAILED, "contract_violation"),
            ],
        )
        self.assertFalse(result.halted)

    def test_invalid_modify_outputs_halt_without_being_used(self):
        cases = {
            "not_envelope": lambda e, reg: {"payload": {}},
            "wrong_producer": lambda e, reg: dataclasses.replace(
                derive_hook_envelope(e, reg, envelope_id="o"), producer_id="other.hook"
            ),
            "core_producer": lambda e, reg: dataclasses.replace(
                derive_hook_envelope(e, reg, envelope_id="o"), producer_kind="core"
            ),
            "wrong_parent": lambda e, reg: dataclasses.replace(
                derive_hook_envelope(e, reg, envelope_id="o"), parent_envelope_id="x"
            ),
            "changed_stage": lambda e, reg: derive_hook_envelope(
                e, reg, stage="reasoning", envelope_id="o"
            ),
            "changed_schema": lambda e, reg: derive_hook_envelope(
                e, reg, payload_schema="other.schema", envelope_id="o"
            ),
            "changed_trace": lambda e, reg: dataclasses.replace(
                derive_hook_envelope(e, reg, envelope_id="o"), trace_id="trace-2"
            ),
            "changed_frame": lambda e, reg: dataclasses.replace(
                derive_hook_envelope(e, reg, envelope_id="o"), frame_id="frame-9"
            ),
            "changed_correlation": lambda e, reg: dataclasses.replace(
                derive_hook_envelope(e, reg, envelope_id="o"), correlation_id="c-9"
            ),
            "added_artifact_ref": lambda e, reg: dataclasses.replace(
                derive_hook_envelope(e, reg, envelope_id="o"),
                artifact_refs=(
                    ArtifactReference(
                        "sha256." + "a" * 64, "a" * 64, "image/png", False
                    ),
                ),
            ),
        }
        for name, build in cases.items():
            with self.subTest(case=name):
                runtime = runtime_with_state_point()
                holder = {}
                holder["a.mod"] = registration(
                    "a.mod", HookMode.MODIFY, lambda e: build(e, holder["a.mod"])
                )
                runtime.register(holder["a.mod"])
                calls = []
                runtime.register(registration("b.read", HookMode.READ, calls.append))
                source = make_envelope(envelope_id="env-2")

                with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
                    result = runtime.dispatch(source)

                self.assertTrue(result.halted)
                self.assertEqual(result.halt_reasons, ("a.mod:contract_violation",))
                self.assertIs(result.envelope, source)
                self.assertEqual(calls, [])

    def test_forged_invalid_envelope_is_rejected_by_revalidation(self):
        runtime = runtime_with_state_point()
        holder = {}

        def forge(envelope):
            output = derive_hook_envelope(envelope, holder["a.mod"], envelope_id="o")
            object.__setattr__(output, "confidence", 7.0)
            return output

        holder["a.mod"] = registration(
            "a.mod", HookMode.MODIFY, forge, on_failure=HookFailurePolicy.SKIP
        )
        runtime.register(holder["a.mod"])
        source = make_envelope()

        with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
            result = runtime.dispatch(source)

        self.assertFalse(result.halted)
        self.assertIs(result.envelope, source)
        self.assertEqual(result.records[0].failure_kind, "invalid_output")
        self.assertEqual(result.records[0].error_code, "malformed")

    def test_output_byte_budget_is_enforced(self):
        runtime = runtime_with_state_point()
        holder = {}

        def modify(envelope):
            return derive_hook_envelope(
                envelope, holder["a.mod"], payload={"text": "x" * 2048}, envelope_id="o"
            )

        holder["a.mod"] = registration(
            "a.mod", HookMode.MODIFY, modify, max_output_bytes=1024
        )
        runtime.register(holder["a.mod"])

        with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
            result = runtime.dispatch(make_envelope())

        self.assertTrue(result.halted)
        self.assertEqual(result.records[0].error_code, "limit_exceeded")

    def test_inject_rules_and_budgets(self):
        def injecting(builder, **overrides):
            runtime = runtime_with_state_point(**overrides)
            holder = {}
            holder["a.inj"] = registration(
                "a.inj",
                HookMode.INJECT,
                lambda e: builder(e, holder["a.inj"]),
                max_injected=2,
            )
            runtime.register(holder["a.inj"])
            with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
                return runtime.dispatch(make_envelope())

        safety = injecting(
            lambda e, reg: [
                derive_hook_envelope(e, reg, stage="safety", envelope_id="o")
            ]
        )
        execution = injecting(
            lambda e, reg: [
                derive_hook_envelope(e, reg, stage="execution", envelope_id="o")
            ]
        )
        too_many = injecting(
            lambda e, reg: [
                derive_hook_envelope(e, reg, envelope_id=f"o{i}") for i in range(3)
            ]
        )
        over_total = injecting(
            lambda e, reg: [derive_hook_envelope(e, reg, envelope_id="o")],
            max_injected_total=0,
        )
        duplicate = injecting(
            lambda e, reg: [
                derive_hook_envelope(e, reg, envelope_id="o"),
                derive_hook_envelope(e, reg, envelope_id="o"),
            ]
        )
        generator = injecting(
            lambda e, reg: (
                derive_hook_envelope(e, reg, envelope_id=f"o{i}") for i in range(1)
            )
        )
        for name, result in (
            ("safety", safety),
            ("execution", execution),
            ("too_many", too_many),
            ("over_total", over_total),
            ("duplicate", duplicate),
            ("generator", generator),
        ):
            with self.subTest(case=name):
                self.assertFalse(result.halted)
                self.assertEqual(result.injected, ())
                self.assertEqual(result.records[0].failure_kind, "contract_violation")

    def test_timeout_is_isolated_and_quarantines_the_hook(self):
        release = threading.Event()
        runtime = runtime_with_state_point()
        runtime.register(
            registration(
                "a.slow",
                HookMode.READ,
                lambda _: release.wait(5),
                timeout_ns=20_000_000,
            )
        )
        calls = []
        runtime.register(registration("b.read", HookMode.READ, calls.append))
        try:
            with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
                first = runtime.dispatch(make_envelope())
            second = runtime.dispatch(make_envelope())
        finally:
            release.set()

        self.assertEqual(first.records[0].status, HOOK_STATUS_TIMEOUT)
        self.assertGreaterEqual(first.records[0].elapsed_ns, 20_000_000)
        self.assertEqual(first.records[1].status, HOOK_STATUS_COMPLETED)
        self.assertEqual(second.records[0].status, HOOK_STATUS_QUARANTINED)
        self.assertEqual(len(calls), 2)
        self.assertFalse(first.halted)

    def test_halting_hook_timeout_stops_the_boundary(self):
        release = threading.Event()
        runtime = runtime_with_state_point()
        runtime.register(
            registration(
                "a.required",
                HookMode.READ,
                lambda _: release.wait(5),
                timeout_ns=20_000_000,
                on_failure=HookFailurePolicy.HALT,
            )
        )
        try:
            with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
                first = runtime.dispatch(make_envelope())
            second = runtime.dispatch(make_envelope())
        finally:
            release.set()

        self.assertTrue(runtime.has_halting_hooks("state", "output"))
        self.assertTrue(first.halted)
        self.assertEqual(first.halt_reasons, ("a.required:timeout",))
        self.assertTrue(second.halted)
        self.assertEqual(second.halt_reasons, ("a.required:quarantined",))

    def test_repeated_exceptions_quarantine_and_success_resets_count(self):
        outcomes = iter([True, False, True, True])

        def flaky(_):
            if next(outcomes):
                raise RuntimeError("boom")

        runtime = runtime_with_state_point(max_consecutive_failures=2)
        runtime.register(registration("a.flaky", HookMode.READ, flaky))
        statuses = []
        with self.assertLogs("ai_game_player.hook_runtime", "WARNING") as logs:
            for _ in range(5):
                statuses.append(runtime.dispatch(make_envelope()).records[0].status)

        self.assertEqual(
            statuses,
            [
                HOOK_STATUS_FAILED,
                HOOK_STATUS_COMPLETED,
                HOOK_STATUS_FAILED,
                HOOK_STATUS_FAILED,
                HOOK_STATUS_QUARANTINED,
            ],
        )
        self.assertIn("error_type=RuntimeError", logs.output[0])
        self.assertNotIn("boom", "\n".join(logs.output))

    def test_measures_latency_per_hook_and_dispatch(self):
        ticks = iter(range(0, 10_000, 100))
        runtime = runtime_with_state_point(clock_ns=lambda: next(ticks))
        runtime.register(registration("a.read", HookMode.READ, lambda _: None))
        runtime.register(registration("b.read", HookMode.READ, lambda _: None))

        result = runtime.dispatch(make_envelope())

        self.assertEqual([r.elapsed_ns for r in result.records], [100, 100])
        self.assertEqual(result.elapsed_ns, 500)
        summary = result.to_dict()
        self.assertEqual(summary["records"][0]["hook_id"], "a.read")
        self.assertNotIn("payload", summary)

    def test_hook_exception_never_reaches_the_core_thread(self):
        runtime = runtime_with_state_point()

        def exits(_):
            raise SystemExit(3)

        runtime.register(registration("a.exit", HookMode.READ, exits))
        with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
            result = runtime.dispatch(make_envelope())
        self.assertEqual(result.records[0].failure_kind, "exception")


if __name__ == "__main__":
    unittest.main()
