"""Issue #786 SessionResourceRegistry / CleanupCoordinator tests (T1-T15)."""

import json
import threading
import unittest

from ai_game_player.run_control import ExecutionCancelled
from ai_game_player.runtime.resource_lifecycle import (
    CleanupCoordinator,
    ClosableOwnerAdapter,
    ResourceLifecycleError,
    ResourcePressureSnapshot,
    ResourceScope,
    ResourceState,
    SessionResourceRegistry,
)


class Clock:
    def __init__(self):
        self.now = 1_000_000_000

    def __call__(self):
        return self.now

    def advance_ms(self, ms):
        self.now += int(ms * 1_000_000)


class Owner:
    """Fake owner: records release calls (shared log for ordering)."""

    def __init__(self, log, fail=(), clock=None, cost_ms=0.0):
        self.log = log
        self.fail = set(fail)
        self.clock = clock
        self.cost_ms = cost_ms

    def release(self, resource_id):
        self.log.append(resource_id)
        if self.clock is not None:
            self.clock.advance_ms(self.cost_ms)
        if resource_id in self.fail:
            raise OSError("secret-path-should-not-leak")


class Sink:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def record(self, event, payload):
        if self.fail:
            raise RuntimeError("sink down")
        self.events.append((event, dict(payload)))


S, G, T = ResourceScope.SESSION, ResourceScope.GENERATION, ResourceScope.TASK


def code_of(case, call):
    with case.assertRaises(ResourceLifecycleError) as raised:
        call()
    return raised.exception.code


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.log = []
        self.sink = Sink()
        self.owner = Owner(self.log)
        self.reg = SessionResourceRegistry(
            "s1", clock_ns=self.clock, trace_sink=self.sink
        )

    def add(self, rid, scope=S, kind="frame.shared", **kw):
        return self.reg.acquire(rid, kind, self.owner, owner_scope=scope, **kw)

    def test_t1_normal_release(self):
        self.add("a", estimated_bytes=10)
        self.assertEqual(self.reg.release("a"), ResourceState.RELEASED)
        snap = self.reg.snapshot()
        self.assertEqual(self.log, ["a"])
        self.assertEqual(snap.active_count, 0)
        self.assertEqual(snap.counters["released"], 1)
        self.assertEqual(snap.active_by_kind, {})

    def test_t2_double_release(self):
        self.add("a")
        self.reg.release("a")
        self.assertEqual(self.reg.release("a"), ResourceState.RELEASED)
        self.assertEqual(self.log, ["a"])
        self.assertEqual(self.reg.snapshot().counters["double_release"], 1)
        self.assertIsNone(self.reg.release("never"))
        # 並行2threadでもreleaserは1回
        self.add("b")
        barrier = threading.Barrier(2)

        def go():
            barrier.wait()
            self.reg.release("b")

        threads = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(self.log.count("b"), 1)

    def test_t3_task_cancel_timeout_exception(self):
        for exc in (ExecutionCancelled("c"), TimeoutError("t"), ValueError("v")):
            rid = "t-" + type(exc).__name__
            with self.assertRaises(type(exc)):
                with self.reg.task_scope("step-1"):
                    self.add(rid, T)
                    raise exc
            self.assertIn(rid, self.log)
            self.assertEqual(self.reg.snapshot().active_count, 0)
        self.assertEqual(code_of(self, lambda: self.add("x", T)), "no_active_task")
        with self.reg.task_scope("outer"):
            self.assertEqual(
                code_of(self, lambda: self.reg.task_scope("outer").__enter__()),
                "invalid_lease",
            )

    def test_t4_generation_rollover(self):
        self.add("g0", G)
        with self.reg.task_scope("k"):
            self.add("t0", T)
            self.add("s0", S)
            report = self.reg.advance_generation(1)
            self.assertEqual(report.trigger, "generation")
        self.assertEqual(sorted(self.log), ["g0", "t0"])
        self.assertEqual(self.reg.snapshot().active_count, 1)
        self.assertEqual(
            code_of(self, lambda: self.reg.advance_generation(1)), "stale_generation"
        )
        self.assertEqual(self.reg.current_generation, 1)

    def test_t5_close_session_order(self):
        self.add("s1", S)
        self.add("s2", S)
        self.add("g1", G)
        with self.reg.task_scope("k"):
            self.add("t1", T)
            self.add("t2", T)
            self.reg.close_session()
        self.assertEqual(self.log, ["t2", "t1", "g1", "s2", "s1"])
        self.assertEqual(code_of(self, lambda: self.add("n", S)), "registry_closed")
        self.assertEqual(
            code_of(self, lambda: self.reg.borrow("s1", expected_generation=0)),
            "registry_closed",
        )
        self.assertEqual(self.reg.snapshot().session_state, "CLOSED")

    def test_t6_pinned(self):
        self.add("p", pinned=True)
        self.add("e", evictable=True)
        self.assertEqual(
            code_of(self, lambda: self.add("bad", pinned=True, ttl_ms=5)),
            "invalid_lease",
        )
        self.assertEqual(
            code_of(self, lambda: self.add("bad", pinned=True, evictable=True)),
            "invalid_lease",
        )
        snapshot = ResourcePressureSnapshot("critical", 1, "test")
        report = CleanupCoordinator(self.reg).on_pressure(snapshot)
        self.assertEqual((report.skipped_pinned, report.released), (1, 1))
        self.assertEqual(self.log, ["e"])
        self.reg.close_session()
        self.assertIn("p", self.log)  # scope終了ではpinnedも解放

    def test_t7_pressure_order_and_bounds(self):
        coordinator = CleanupCoordinator(self.reg)
        for i in range(10):
            self.clock.advance_ms(1)
            self.add(
                f"r{i}",
                evictable=True,
                priority=900 - i if i % 2 == 0 else 100,
                estimated_bytes=100,
                ttl_ms=3 if i == 7 else None,
            )
        self.clock.advance_ms(10)  # r7だけ期限切れ
        self.add("np", kind="other.kind")
        self.add("busy", evictable=True, priority=0)
        with self.reg.borrow("busy", expected_generation=0):
            report = coordinator.on_pressure(
                ResourcePressureSnapshot("elevated", 1, "t")
            )
        self.assertTrue(report.truncated)
        self.assertEqual(report.released, 8)
        self.assertEqual(report.skipped_in_use, 1)
        self.assertEqual(report.skipped_not_evictable, 1)
        # 期限切れ(r7) → priority小(100: r1,r3,r5,r9 古い順) → priority大(900-i の小さい順)
        self.assertEqual(self.log[:5], ["r7", "r1", "r3", "r5", "r9"])
        self.assertEqual(self.log[5:], ["r8", "r6", "r4"])

    def test_t7_target_and_none_and_invalid(self):
        coordinator = CleanupCoordinator(self.reg)
        for i in range(4):
            self.clock.advance_ms(1)
            self.add(f"r{i}", evictable=True, estimated_bytes=100)
        none = coordinator.on_pressure(
            ResourcePressureSnapshot("none", 1, "t", reclaim_target_bytes=1)
        )
        self.assertEqual(none.attempted, 0)
        met = coordinator.on_pressure(
            ResourcePressureSnapshot("critical", 1, "t", reclaim_target_bytes=150)
        )
        self.assertEqual(
            (met.released, met.target_met, met.truncated), (2, True, False)
        )
        short = coordinator.on_pressure(
            ResourcePressureSnapshot("critical", 1, "t", reclaim_target_bytes=10_000)
        )
        self.assertEqual(
            (short.released, short.target_met, short.truncated), (2, False, False)
        )
        for bad in (
            dict(schema_version=2),
            dict(level="boom"),
            dict(reclaim_target_bytes=-1),
            dict(reclaim_target_bytes=float("nan")),
            dict(observed_monotonic_ns=float("inf")),
        ):
            values = dict(level="critical", observed_monotonic_ns=1, source="t")
            values.update(bad)
            with self.assertRaises(ValueError):
                ResourcePressureSnapshot(**values)
        with self.assertRaises(ValueError):
            coordinator.cleanup_expired(max_items=0)
        with self.assertRaises(ValueError):
            coordinator.cleanup_expired(budget_ms=float("nan"))

    def test_expired_cleanup(self):
        coordinator = CleanupCoordinator(self.reg)
        self.add("x", ttl_ms=5)
        self.add("y", ttl_ms=5)
        self.add("z", ttl_ms=500)
        self.clock.advance_ms(5)  # now >= expires の境界
        with self.reg.borrow("y", expected_generation=0):
            report = coordinator.cleanup_expired()
        self.assertEqual((report.released, report.skipped_in_use), (1, 1))
        self.assertEqual(self.log, ["x"])
        self.assertEqual(coordinator.cleanup_expired().released, 1)  # yは次回に回った
        self.assertEqual(self.reg.snapshot().counters["expired"], 2)

    def test_t8_release_failure_and_budget(self):
        owner = Owner(self.log, fail={"b"})
        for rid in "abc":
            self.reg.acquire(rid, "k.x", owner, owner_scope=S)
        report = self.reg.close_session()
        self.assertEqual((report.released, report.failed), (2, 1))
        failed = [e for e in self.sink.events if e[0] == "resource.release_failed/v1"]
        self.assertEqual(len(failed), 1)
        self.assertNotIn("secret", json.dumps(failed))
        self.assertEqual(self.log, ["c", "b", "a"])  # 失敗しても他は解放、再試行なし
        self.reg.close_session()
        self.assertEqual(self.log, ["c", "b", "a"])
        self.assertEqual(
            self.reg.snapshot().recent_failures[0]["error_type"], "OSError"
        )

    def test_t8_truncated_close_resumes(self):
        slow = Owner(self.log, clock=self.clock, cost_ms=30)
        reg = SessionResourceRegistry("s2", clock_ns=self.clock)
        for rid in "abc":
            reg.acquire(rid, "k.x", slow, owner_scope=S)
        first = reg.close_session(budget_ms=50)
        self.assertTrue(first.truncated)
        self.assertEqual(first.released, 2)
        second = reg.close_session(budget_ms=50)
        self.assertEqual(second.released, 1)
        self.assertEqual(sorted(self.log), ["a", "b", "c"])  # 各1回

    def test_running_entry_not_released_twice_by_concurrent_close(self):
        gate, entered = threading.Event(), threading.Event()
        calls = []

        class Blocking:
            def release(self, resource_id):
                calls.append(resource_id)
                entered.set()
                gate.wait(5)

        self.reg.acquire("a", "k.x", Blocking(), owner_scope=S)
        worker = threading.Thread(target=self.reg.close_session)
        worker.start()
        entered.wait(5)
        self.reg.close_session()
        gate.set()
        worker.join()
        self.assertEqual(calls, ["a"])

    def test_t9_stale_borrow_and_closed_generation(self):
        self.reg.advance_generation(1)
        self.add("g", G)
        self.assertEqual(
            code_of(self, lambda: self.reg.borrow("g", expected_generation=0)),
            "stale_generation",
        )
        self.assertEqual(
            code_of(self, lambda: self.add("h", G, generation=0)), "stale_generation"
        )
        with self.reg.borrow("g", expected_generation=1) as lease:
            self.assertEqual(lease.generation, 1)
        self.reg.release("g")
        self.assertEqual(
            code_of(self, lambda: self.reg.borrow("g", expected_generation=1)),
            "not_active",
        )
        self.reg.close_generation(1)
        self.assertEqual(code_of(self, lambda: self.add("i", G)), "generation_closed")
        self.reg.advance_generation(2)
        self.add("j", G)

    def test_t9_concurrent_advance_does_not_touch_new_generation(self):
        stop = threading.Event()
        errors = []

        def churn():
            n = 0
            while not stop.is_set():
                n += 1
                try:
                    lease = self.add(f"c{n}", G)
                    with self.reg.borrow(
                        lease.resource_id, expected_generation=lease.generation
                    ):
                        pass  # 成功した借用は借用時点でcurrent世代のものだけ
                    self.reg.release(lease.resource_id)
                except ResourceLifecycleError as exc:
                    if exc.code not in ("stale_generation", "not_active"):
                        errors.append(exc.code)

        worker = threading.Thread(target=churn)
        worker.start()
        for generation in range(1, 40):
            self.reg.advance_generation(generation)
        stop.set()
        worker.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.reg.current_generation, 39)

    def test_t10_unbounded_growth(self):
        for i in range(10_000):
            self.add(
                "same" if i % 2 else f"id{i}",
                kind="k.x",
                estimated_bytes=1,
                ttl_ms=1000,
            )
            self.reg.release("same" if i % 2 else f"id{i}")
        snap = self.reg.snapshot()
        self.assertEqual((snap.active_count, snap.active_by_kind), (0, {}))
        self.assertLessEqual(len(self.reg._terminal), 256)
        self.assertLessEqual(len(self.reg._expiry), 64)

    def test_t11_deterministic(self):
        def run():
            log = []
            clock = Clock()
            reg = SessionResourceRegistry("d", clock_ns=clock)
            owner = Owner(log)
            for i in range(6):
                clock.advance_ms(1)
                reg.acquire(
                    f"r{i}",
                    "k.x",
                    owner,
                    owner_scope=(S, G, T)[i % 3] if i % 3 < 2 else G,
                    evictable=True,
                )
            report = reg.close_session()
            return log, report.to_payload()

        a, b = run(), run()
        self.assertEqual(a[0], b[0])
        self.assertEqual({**a[1], "latency_ms": 0}, {**b[1], "latency_ms": 0})

    def test_t12_borrow_defers_release(self):
        self.add("a")
        with self.reg.borrow("a", expected_generation=0):
            self.assertEqual(self.reg.release("a"), ResourceState.RELEASING)
            self.assertEqual(self.log, [])
            report = self.reg.close_session()
            self.assertEqual(report.deferred, 1)
        self.assertEqual(self.log, ["a"])

    def test_t13_adapters(self):
        calls = []

        class RefOwner:
            def release(self, ref):
                calls.append(("release", ref))

        class CacheOwner:
            def evict(self, key):
                calls.append(("evict", key))

        class Closer:
            def close(self):
                calls.append(("close",))

        class Shutter:
            def shutdown(self):
                calls.append(("shutdown",))

        class Both(Closer):
            def shutdown(self):
                calls.append(("shutdown",))

        class EvictAdapter:
            def __init__(self, cache):
                self.cache = cache

            def release(self, rid):
                self.cache.evict(rid)

        self.reg.acquire("f", "frame.shared", RefOwner(), owner_scope=S)
        self.reg.acquire("m", "gpu.model", EvictAdapter(CacheOwner()), owner_scope=G)
        self.reg.acquire(
            "c", "capture.wgc", ClosableOwnerAdapter(Closer()), owner_scope=S
        )
        self.reg.acquire(
            "w", "capture.wgc", ClosableOwnerAdapter(Shutter()), owner_scope=S
        )
        self.reg.acquire(
            "b", "capture.wgc", ClosableOwnerAdapter(Both()), owner_scope=S
        )
        self.reg.close_session()
        self.assertEqual(
            sorted(calls),
            sorted(
                [
                    ("release", "f"),
                    ("evict", "m"),
                    ("close",),
                    ("shutdown",),
                    ("close",),
                ]
            ),
        )
        with self.assertRaises(TypeError):
            ClosableOwnerAdapter(object())
        self.assertEqual(
            code_of(
                self,
                lambda: SessionResourceRegistry("x").acquire(
                    "p", "k.x", self.owner, owner_scope=ResourceScope.PROCESS
                ),
            ),
            "scope_not_supported",
        )

    def test_t14_trace_payload(self):
        self.add("a", estimated_bytes=64)
        self.add("b")
        self.reg.close_session()
        event, payload = [e for e in self.sink.events if e[0] == "resource.cleanup/v1"][
            0
        ]
        self.assertEqual(
            (
                payload["trigger"],
                payload["released"],
                payload["freed_estimated_bytes"],
                payload["unknown_bytes_count"],
            ),
            ("session", 2, 64, 1),
        )
        json.dumps(payload)
        json.dumps(self.reg.snapshot().to_payload())
        # sink例外でcleanupは止まらない
        broken = SessionResourceRegistry("b", trace_sink=Sink(fail=True))
        broken.acquire("a", "k.x", self.owner, owner_scope=S)
        broken.acquire("b", "k.x", Owner([], fail={"b"}), owner_scope=S)
        report = broken.close_session()
        self.assertEqual((report.released, report.failed), (1, 1))
        self.assertGreaterEqual(broken.snapshot().counters["trace_failed"], 1)

    def test_t15_hot_path_does_not_scan_history(self):
        for i in range(300):
            self.add(f"h{i}", ttl_ms=100000)
            self.reg.release(f"h{i}")
        self.add("live", kind="k.live", ttl_ms=100000)
        before = self.reg._stats_updates
        self.reg.snapshot()
        self.reg.release("live")
        self.assertEqual(self.reg._stats_updates - before, 1)

    def test_expired_candidates_beyond_limit_stay_in_heap(self):
        coordinator = CleanupCoordinator(self.reg)
        for i in range(5):
            self.add(f"x{i}", ttl_ms=1)
        self.clock.advance_ms(2)
        first = coordinator.cleanup_expired(max_items=2)
        self.assertEqual((first.released, first.truncated), (2, True))
        self.assertEqual(coordinator.cleanup_expired(max_items=10).released, 3)
        self.assertEqual(self.reg.snapshot().active_count, 0)

    def test_validation_and_capacity(self):
        for bad in (
            dict(max_active=0),
            dict(max_active=70000),
            dict(terminal_history=-1),
            dict(terminal_history=5000),
        ):
            with self.assertRaises(ValueError):
                SessionResourceRegistry("x", **bad)
        small = SessionResourceRegistry("x", max_active=1)
        small.acquire("a", "k.x", self.owner, owner_scope=S)
        self.assertEqual(
            code_of(self, lambda: small.acquire("b", "k.x", self.owner, owner_scope=S)),
            "capacity_exceeded",
        )
        self.assertEqual(
            code_of(self, lambda: small.acquire("a", "k.x", self.owner, owner_scope=S)),
            "duplicate_resource_id",
        )
        for kwargs in (dict(estimated_bytes=-1), dict(ttl_ms=0), dict(priority=1001)):
            self.assertEqual(
                code_of(self, lambda: self.add("v", **kwargs)), "invalid_lease"
            )
        with self.assertRaises(ValueError):
            self.reg.close_session(budget_ms=0)


if __name__ == "__main__":
    unittest.main()
