import copy
import json
import unittest
from pathlib import Path

from plugin_capability_vectors import enforcement_outcome

from ai_game_player.hook_dispatch_result import (
    HOOK_STATUS_COMPLETED,
    HOOK_STATUS_FAILED,
)
from ai_game_player.hook_envelope import derive_hook_envelope
from ai_game_player.hook_registration import HookMode, HookRegistration
from ai_game_player.hook_runtime import HookRuntime
from ai_game_player.plugin_capability_manifest import (
    PluginCapabilityManifest,
    PluginManifestError,
)
from ai_game_player.plugin_capability_policy import (
    PluginCapabilityDenied,
    PluginCapabilityPolicy,
)
from ai_game_player.stage_envelope import StageEnvelope

CONTRACT_DIR = (
    Path(__file__).parents[1] / "schemas" / "plugin_capability_manifest" / "v1"
)
ENFORCEMENT_PATH = CONTRACT_DIR / "enforcement_conformance.json"
EXAMPLE_PATH = CONTRACT_DIR / "examples" / "perception_ocr_hint.json"
INJECT_ID = "plugin.example.ocr_hint.hinter"
READ_ID = "plugin.example.ocr_hint.observer"
DENIAL_CODES = {
    PluginCapabilityDenied.MANIFEST_MISSING,
    PluginCapabilityDenied.VERSION_MISMATCH,
    PluginCapabilityDenied.PERMISSION_UNAVAILABLE,
    PluginCapabilityDenied.MODE_NOT_GRANTED,
    PluginCapabilityDenied.POINT_NOT_GRANTED,
    PluginCapabilityDenied.BUDGET_EXCEEDED,
    PluginCapabilityDenied.PAYLOAD_SCHEMA_NOT_GRANTED,
    PluginCapabilityDenied.OUTPUT_NOT_GRANTED,
    PluginCapabilityDenied.ARTIFACT_REF_NOT_GRANTED,
}


def example_document() -> dict:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


def modify_document() -> dict:
    document = example_document()
    hook = document["hooks"][1]
    hook["mode"] = "modify"
    document["permissions"] = ["hook.modify"]
    document["hooks"] = [hook]
    return document


def make_policy(document=None) -> PluginCapabilityPolicy:
    return PluginCapabilityPolicy(
        [PluginCapabilityManifest.from_dict(document or example_document())]
    )


def make_envelope(**overrides) -> StageEnvelope:
    values = {
        "envelope_id": "env-1",
        "stage": "perception",
        "boundary": "output",
        "session_id": "session-1",
        "timestamp_utc": "2026-11-04T00:00:00Z",
        "trace_id": "trace-1",
        "producer_kind": "core",
        "producer_id": "core.decision_pipeline",
        "producer_version": "1",
        "payload_schema": "kadoka.perception.candidates",
        "payload_schema_version": 1,
        "payload": {"candidate_count": 0},
    }
    values.update(overrides)
    return StageEnvelope(**values)


def registration(hook_id, mode, callback, **overrides) -> HookRegistration:
    values = {
        "hook_id": hook_id,
        "hook_version": "0.3.1",
        "mode": mode,
        "stage": "perception",
        "boundary": "output",
        "callback": callback,
        "timeout_ns": 20_000_000,
        "max_output_bytes": 1,
    }
    if mode is HookMode.INJECT:
        values.update(timeout_ns=50_000_000, max_output_bytes=65_536)
    values.update(overrides)
    return HookRegistration(**values)


def runtime_for(policy) -> HookRuntime:
    runtime = HookRuntime(enabled=True, access_policy=policy)
    runtime.declare_point(
        "perception", "output", (HookMode.READ, HookMode.MODIFY, HookMode.INJECT)
    )
    return runtime


class EnforcementConformanceTest(unittest.TestCase):
    def test_reference_policy_matches_every_enforcement_vector(self):
        cases = json.loads(ENFORCEMENT_PATH.read_text(encoding="utf-8"))["cases"]
        self.assertGreaterEqual(len(cases), 25)
        self.assertEqual(len(cases), len({case["name"] for case in cases}))
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(case["expect"], enforcement_outcome(case))

    def test_vectors_cover_every_denial_and_policy_code(self):
        cases = json.loads(ENFORCEMENT_PATH.read_text(encoding="utf-8"))["cases"]

        self.assertEqual(
            DENIAL_CODES
            | {
                "allow",
                PluginManifestError.INCOMPATIBLE,
                PluginManifestError.CONFLICT,
            },
            {case["expect"] for case in cases},
        )


class PluginCapabilityPolicyTest(unittest.TestCase):
    def test_add_revalidates_directly_constructed_manifests(self):
        manifest = PluginCapabilityManifest.from_dict(example_document())
        forged = copy.copy(manifest)
        object.__setattr__(forged, "permissions", ("hook.read", "input.direct"))

        with self.assertRaises(PluginManifestError) as raised:
            PluginCapabilityPolicy([forged])
        self.assertEqual(
            PluginManifestError.FORBIDDEN_PERMISSION, raised.exception.code
        )
        with self.assertRaises(PluginManifestError) as raised:
            PluginCapabilityPolicy().add(example_document())  # type: ignore[arg-type]
        self.assertEqual(PluginManifestError.MALFORMED, raised.exception.code)

    def test_conflict_does_not_replace_existing_grants(self):
        policy = make_policy()
        widened = example_document()
        widened["hooks"][0]["budget"]["timeout_ns"] = 1_000_000_000
        with self.assertRaises(PluginManifestError):
            policy.add(PluginCapabilityManifest.from_dict(widened))

        with self.assertRaises(PluginCapabilityDenied) as raised:
            policy.authorize_registration(
                registration(
                    INJECT_ID, HookMode.INJECT, lambda e: [], timeout_ns=900_000_000
                )
            )
        self.assertEqual(PluginCapabilityDenied.BUDGET_EXCEEDED, raised.exception.code)

    def test_records_registration_decisions_without_payload(self):
        policy = make_policy()
        manifest_digest = PluginCapabilityManifest.from_dict(
            example_document()
        ).digest()
        policy.authorize_registration(registration(READ_ID, HookMode.READ, print))
        with self.assertRaises(PluginCapabilityDenied):
            policy.authorize_registration(
                registration(READ_ID, HookMode.READ, print, hook_version="9")
            )
        with self.assertRaises(PluginCapabilityDenied):
            policy.authorize_registration(
                registration("plugin.unknown", HookMode.READ, print)
            )

        decisions = [decision.to_dict() for decision in policy.decisions]

        self.assertEqual([True, False, False], [d["allowed"] for d in decisions])
        self.assertEqual(
            [None, "version_mismatch", "manifest_missing"],
            [d["code"] for d in decisions],
        )
        self.assertEqual(manifest_digest, decisions[1]["manifest_sha256"])
        self.assertEqual("0.3.1", decisions[1]["plugin_version"])
        self.assertIsNone(decisions[2]["plugin_id"])
        self.assertEqual("9", decisions[1]["hook_version"])

    def test_decision_log_is_bounded(self):
        policy = PluginCapabilityPolicy(max_decisions=2)
        for _ in range(3):
            with self.assertRaises(PluginCapabilityDenied):
                policy.authorize_registration(
                    registration(READ_ID, HookMode.READ, print)
                )
        self.assertEqual(2, len(policy.decisions))
        with self.assertRaises(ValueError):
            PluginCapabilityPolicy(max_decisions=0)


class HookRuntimeEnforcementTest(unittest.TestCase):
    def test_deny_by_default_rejects_undeclared_hooks(self):
        runtime = runtime_for(PluginCapabilityPolicy())

        with self.assertRaises(PluginCapabilityDenied) as raised:
            runtime.register(registration(READ_ID, HookMode.READ, lambda e: None))
        self.assertEqual(PluginCapabilityDenied.MANIFEST_MISSING, raised.exception.code)
        self.assertFalse(runtime.has_hooks("perception", "output"))

    def test_declared_hooks_run_and_inject_declared_outputs(self):
        runtime = runtime_for(make_policy())
        seen = []
        holder = {}
        runtime.register(registration(READ_ID, HookMode.READ, seen.append))
        holder["inject"] = registration(
            INJECT_ID,
            HookMode.INJECT,
            lambda e: [
                derive_hook_envelope(
                    e,
                    holder["inject"],
                    payload_schema="plugin.example.ocr_hint",
                    payload={"hint": "start"},
                    envelope_id="hint-1",
                )
            ],
        )
        runtime.register(holder["inject"])

        result = runtime.dispatch(make_envelope())

        self.assertEqual(1, len(seen))
        self.assertEqual(("hint-1",), tuple(e.envelope_id for e in result.injected))
        self.assertEqual(
            [HOOK_STATUS_COMPLETED, HOOK_STATUS_COMPLETED],
            [record.status for record in result.records],
        )

    def test_undeclared_payload_schema_is_never_passed_to_the_hook(self):
        runtime = runtime_for(make_policy())
        seen = []
        runtime.register(registration(READ_ID, HookMode.READ, seen.append))

        with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
            result = runtime.dispatch(
                make_envelope(payload_schema="kadoka.state.snapshot")
            )
            again = runtime.dispatch(make_envelope(payload_schema_version=2))

        self.assertEqual([], seen)
        for outcome in (result, again):
            record = outcome.records[0]
            self.assertEqual(HOOK_STATUS_FAILED, record.status)
            self.assertEqual("capability_denied", record.failure_kind)
            self.assertEqual("payload_schema_not_granted", record.error_code)
            self.assertEqual(0, record.elapsed_ns)
        # 入力の拒否はHookの失敗ではないため、quarantineにしない。
        runtime.dispatch(make_envelope())
        self.assertEqual(1, len(seen))

    def test_denied_modify_input_halts_the_boundary(self):
        runtime = runtime_for(make_policy(modify_document()))
        runtime.register(registration(READ_ID, HookMode.MODIFY, lambda e: e))

        with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
            result = runtime.dispatch(make_envelope(payload_schema_version=2))

        self.assertTrue(result.halted)
        self.assertEqual((f"{READ_ID}:capability_denied",), result.halt_reasons)

    def test_undeclared_inject_outputs_are_discarded(self):
        runtime = runtime_for(make_policy())
        holder = {}
        outputs = {
            "schema": dict(payload_schema="plugin.other"),
            "stage": dict(payload_schema="plugin.example.ocr_hint", stage="decision"),
        }
        choice = {}

        def inject(envelope):
            return [
                derive_hook_envelope(
                    envelope, holder["inject"], envelope_id="o", **outputs[choice["k"]]
                )
            ]

        holder["inject"] = registration(INJECT_ID, HookMode.INJECT, inject)
        runtime.register(holder["inject"])
        for key in outputs:
            choice["k"] = key
            with self.subTest(case=key):
                with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
                    result = runtime.dispatch(make_envelope())
                self.assertEqual((), result.injected)
                self.assertEqual("capability_denied", result.records[0].failure_kind)
                self.assertEqual("output_not_granted", result.records[0].error_code)

    def test_policy_errors_fail_closed_and_are_not_reported_as_denials(self):
        class CodedFailure(RuntimeError):
            code = "payload_schema_not_granted"

        failures = (
            RuntimeError("policy store unavailable"),
            CodedFailure("unexpected"),
            PluginCapabilityDenied("Not A Code", "bad code text"),
        )
        for failure in failures:

            class BrokenPolicy:
                def authorize_registration(self, registration):
                    return None

                def authorize_input(self, registration, envelope, error=failure):
                    raise error

                def authorize_output(self, registration, output, source):
                    return None

            runtime = runtime_for(BrokenPolicy())
            seen = []
            runtime.register(registration(READ_ID, HookMode.READ, seen.append))

            with self.subTest(failure=type(failure).__name__):
                with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
                    result = runtime.dispatch(make_envelope())

                self.assertEqual([], seen)
                self.assertEqual("capability_denied", result.records[0].failure_kind)
                self.assertEqual("policy_error", result.records[0].error_code)

    def test_output_policy_errors_discard_the_output(self):
        class RejectingOutputs:
            def authorize_registration(self, registration):
                return None

            def authorize_input(self, registration, envelope):
                return None

            def authorize_output(self, registration, output, source):
                raise KeyError(output.envelope_id)

        runtime = runtime_for(RejectingOutputs())
        holder = {}
        holder["inject"] = registration(
            INJECT_ID,
            HookMode.INJECT,
            lambda e: [derive_hook_envelope(e, holder["inject"], envelope_id="o")],
        )
        runtime.register(holder["inject"])

        with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
            result = runtime.dispatch(make_envelope())

        self.assertEqual((), result.injected)
        self.assertEqual("policy_error", result.records[0].error_code)


class EnvelopeVersionAndPolicyErrorTest(unittest.TestCase):
    def test_envelopes_of_another_schema_version_are_denied(self):
        policy = make_policy()
        reader = registration(READ_ID, HookMode.READ, print)
        policy.authorize_registration(reader)
        forged = make_envelope()
        object.__setattr__(forged, "schema_version", 2)

        with self.assertRaises(PluginCapabilityDenied) as raised:
            policy.authorize_input(reader, forged)
        self.assertEqual(
            PluginCapabilityDenied.ENVELOPE_VERSION_NOT_GRANTED, raised.exception.code
        )
        policy.authorize_input(reader, make_envelope())

    def test_rejects_invalid_runtime_envelope_versions(self):
        for version in (0, -1, True, "1", 1.0):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    PluginCapabilityPolicy(stage_envelope_schema_version=version)

    def test_unreadable_code_attribute_is_a_policy_error(self):
        class BrokenCode(ValueError):
            @property
            def code(self):
                raise RuntimeError("broken")

        class Policy:
            def authorize_registration(self, registration):
                return None

            def authorize_input(self, registration, envelope):
                raise BrokenCode("denied")

            def authorize_output(self, registration, output, source):
                return None

        runtime = runtime_for(Policy())
        seen = []
        runtime.register(registration(READ_ID, HookMode.READ, seen.append))

        with self.assertLogs("ai_game_player.hook_runtime", "WARNING"):
            result = runtime.dispatch(make_envelope())

        self.assertEqual([], seen)
        self.assertEqual("policy_error", result.records[0].error_code)


if __name__ == "__main__":
    unittest.main()
