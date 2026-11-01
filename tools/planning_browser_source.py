"""Opt-in comparison sensor; screen-only, with genuine fresh-capture references.

This optional development harness never reads game internals.
The caller supplies an isolated Playwright page and explicitly enables its input.
"""

from dataclasses import replace
import time
from uuid import uuid4

from tools.check_2048_screen_transition import BrowserScreenSource


class FreshCompletedBrowserSource(BrowserScreenSource):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pipeline = None
        self.source_id = "comparison." + uuid4().hex
        self.frame_sequence = 0
        self.completed = []
        self._completed_receipts = set()
        self.last_state = None
        self.last_observation = None

    def capture(self):
        # Keep the generation from BEFORE capture. An external Stop/rearm must
        # invalidate this frame rather than bless it with a newer generation.
        token = self.pipeline.controller.rearm_token if self.pipeline else None
        captured_at = time.monotonic_ns()
        state = super().capture()
        self.last_state = state
        self.frame_sequence += 1
        observation, candidates = self.cached
        observation = replace(
            observation,
            features={
                **observation.features,
                "capture_metadata": {
                    "source_id": self.source_id,
                    "frame_id": f"{self.source_id}:0:{self.frame_sequence}",
                    "source_generation": 0,
                    "monotonic_ns": captured_at,
                },
            },
        )
        self.cached = observation, candidates
        self.last_observation = observation
        if self.pipeline is not None:
            actual = self.pipeline.complete_execution_observation(
                observation, expected_rearm_token=token
            )
            identity = (
                actual.receipt.snapshot_id,
                actual.receipt.action_id,
                actual.receipt.action_generation,
            ) if actual is not None else None
            if actual is not None and identity not in self._completed_receipts:
                self._completed_receipts.add(identity)
                self.completed.append({
                    "action_id": actual.receipt.action_id,
                    "snapshot_id": actual.receipt.snapshot_id,
                    "generation": actual.receipt.action_generation,
                    "execution_status": actual.receipt.execution_status,
                    "input_dispatch_started": actual.receipt.input_dispatch_started,
                    "eligible_for_learning": actual.receipt.eligible_for_learning,
                    "outcome_status": actual.outcome.status,
                    "state_changed": actual.outcome.state_changed,
                    "after_signature": actual.after_signature,
                })
        return state

    def read_fresh(self):
        self.capture()
        return self.read()
