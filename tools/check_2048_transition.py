"""Compare pure transitions with verified upstream JS and record CPU/heap cost."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import platform
import random
import shutil
import statistics
import subprocess
import time
import tracemalloc

from ai_game_player.game_2048_state import Game2048State
from ai_game_player.game_2048_transition import (
    DIRECTIONS,
    ReferenceGame2048TransitionModel,
)
from tools.reference_game_2048_source import PinnedGameSource


ORACLE_PATH = (
    Path(__file__).resolve().parents[1] / "tests/fixtures/2048/transition_oracle.js"
)


def fixture_cases(seed, boards):
    rng = random.Random(seed)
    cases = []
    expected = []
    model = ReferenceGame2048TransitionModel()
    fixtures = [
        Game2048State(((0,) * 4,) * 4, score=0),
        Game2048State(((1024, 1024, 0, 0), (0,) * 4, (0,) * 4, (0,) * 4), score=100),
        Game2048State(
            ((2048, 2048, 0, 0), (0,) * 4, (0,) * 4, (0,) * 4), score=0, won=True
        ),
        Game2048State(
            ((2048, 2048, 0, 0), (0,) * 4, (0,) * 4, (0,) * 4),
            score=0,
            won=True,
            keep_playing=True,
        ),
        Game2048State(
            ((2, 4, 8, 16), (4, 8, 16, 32), (8, 16, 32, 64), (16, 32, 64, 128)),
            score=0,
            over=True,
        ),
    ]
    fixtures.extend(
        Game2048State(
            tuple(
                tuple(rng.choice((0, 0, 2, 4, 8, 16, 64, 1024)) for _ in range(4))
                for _ in range(4)
            ),
            score=rng.randrange(10000),
        )
        for _ in range(boards)
    )
    for index, state in enumerate(fixtures):
        for direction in DIRECTIONS:
            slide = model.apply_slide(state, direction)
            for operation in ("slide", "move"):
                sample_seed = index * 4 + DIRECTIONS.index(direction)
                local_rng = random.Random(sample_seed)
                draws = [local_rng.random(), local_rng.random()]
                next_state = (
                    slide.state
                    if operation == "slide"
                    else model.sample_spawn(slide, sample_seed)
                )
                cases.append(
                    {
                        "state": asdict(state),
                        "direction": direction,
                        "operation": operation,
                        "draws": draws,
                    }
                )
                expected.append((next_state, slide.reward_delta, slide.changed))
        for operation in ("restart", "continue"):
            local_rng = random.Random(index)
            draws = [local_rng.random() for _ in range(4)]
            cases.append(
                {"state": asdict(state), "operation": operation, "draws": draws}
            )
            next_state = (
                model.restart(index)
                if operation == "restart"
                else model.continue_game(state)
            )
            expected.append((next_state, None, None))
    return cases, expected


def compare_oracle(source_root, cases, expected):
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required for the test-only pinned source oracle")
    result = subprocess.run(
        [node, str(ORACLE_PATH), str(source_root)],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    actual = json.loads(result.stdout)
    if len(actual) != len(expected):
        raise AssertionError("oracle returned an unexpected case count")
    for index, (observed, (state, score_delta, moved)) in enumerate(
        zip(actual, expected)
    ):
        wanted = json.loads(json.dumps(asdict(state)))
        if any(observed[key] != value for key, value in wanted.items()):
            raise AssertionError(
                f"oracle mismatch at case {index}: {cases[index]} expected={wanted} actual={observed}"
            )
        if score_delta is not None and observed["score_delta"] != score_delta:
            raise AssertionError(f"oracle score mismatch at case {index}")
        if moved is not None and observed["moved"] != moved:
            raise AssertionError(f"oracle move mismatch at case {index}")
    return len(actual)


def measure_cost(iterations=1000):
    model = ReferenceGame2048TransitionModel()
    state = Game2048State(
        ((2, 2, 4, 8), (0, 4, 4, 0), (0, 0, 2, 2), (0, 0, 0, 0)), score=0
    )
    slide = model.apply_slide(state, "LEFT")
    timings = {}
    heap = {}
    for name, operation in (
        ("slide", lambda: model.apply_slide(state, "LEFT")),
        ("chance_enumeration", lambda: model.spawn_outcomes(slide)),
        ("sample_spawn", lambda: model.sample_spawn(slide, 479)),
    ):
        samples = []
        for _ in range(iterations):
            started = time.perf_counter_ns()
            operation()
            samples.append((time.perf_counter_ns() - started) / 1000)
        timings[name] = {
            "p50_us": statistics.median(samples),
            "p95_us": sorted(samples)[int(len(samples) * 0.95) - 1],
        }
        tracemalloc.start()
        before = tracemalloc.take_snapshot()
        retained = [operation() for _ in range(100)]
        after = tracemalloc.take_snapshot()
        current, peak = tracemalloc.get_traced_memory()
        stats = after.compare_to(before, "lineno")
        heap[name] = {
            "retained_outputs": len(retained),
            "retained_traced_bytes": current,
            "peak_traced_bytes": peak,
            "net_retained_allocation_count": sum(item.count_diff for item in stats),
        }
        tracemalloc.stop()
    return {
        "iterations": iterations,
        "latency": timings,
        "heap_batch_100": heap,
        "copy_contract": {
            "board_cells": 16,
            "new_board_row_tuples_per_changed_state": 4,
            "full_frame_copies": 0,
            "ffi_crossings": 0,
            "note": "Tuple/cell counts describe the implementation; traced heap includes Python overhead, not memcpy traffic.",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boards", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=479)
    parser.add_argument("--cache", type=Path, default=Path("build/reference-game"))
    parser.add_argument(
        "--output", type=Path, default=Path("build/transition-2048/report.json")
    )
    args = parser.parse_args(argv)
    if not 1 <= args.boards <= 10000:
        parser.error("--boards must be between 1 and 10000")
    source = PinnedGameSource(args.cache)
    root = source.materialize()  # Reverify every locked file, including cached sources.
    cases, expected = fixture_cases(args.seed, args.boards)
    checked = compare_oracle(root, cases, expected)
    report = {
        "schema_version": 1,
        "seed": args.seed,
        "pinned_commit": source.lock["commit"],
        "python": platform.python_version(),
        "platform": platform.platform(),
        "oracle_cases": checked,
        "parity": True,
        "real_play": "not_run: pure transition oracle, no game input",
        "cost": measure_cost(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
