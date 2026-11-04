"""計画workerのinterpreter起動をcompute budgetの外へ出すテスト専用launcher。

製品の``BoundedExpectimaxPlanner``は、``plan()``開始からworker起動・探索・受領まで
を一つのwall-time budgetで数える（この意味論は変えない）。新しいPython processの
起動は、CPUを飽和させた負荷下で数秒かかるため、worker結果を期待するテストが
探索とは無関係に``compute_budget``で終わっていた（Issue #729）。

``warm_worker_launch()``は``plan()``を呼ぶ前にinterpreterを起動して
``ai_game_player.planning_worker``と、その``_execute``が使う製品moduleの
importまで済ませておき、plannerが
``subprocess.Popen``を呼んだ時点でそのprocessへ引数を渡して``main()``を実行させる。
plannerが受け取るのは実processなので、poll・terminate/kill・診断・資源制限は
製品と同じ経路を通る。予算に残るのは、資源制限の適用、登録factory moduleの
import、request読取、探索、受領。factory moduleは先読みせず、従来どおり資源制限の
後にworkerがimportする。

打切り・停止・遅延の検証はこのlauncherを使わず、通常の起動経路で行う。
"""

from contextlib import contextmanager
import json
import os
import subprocess
import sys
import threading
from typing import Iterator
from unittest.mock import patch

_REAL_POPEN = subprocess.Popen
_WORKER_COMMAND = [sys.executable, "-m", "ai_game_player.planning_worker"]
_WARM_MARKER = b"planning-warm-worker-ready\n"
# 起動待ちはbudgetの外。異常時にテストが止まり続けないための上限だけを置く。
_WARM_START_TIMEOUT_S = 120


# {
#   責務: [warm_worker_launch: 予め起動したworker interpreterを一回だけplannerへ渡す]
#   処理: [起動とimport完了をbudget外で待ち、worker以外のcommandは実Popenへ委ねる。未使用processは終了時に回収]
#   引数: [なし] 戻り値: [Iterator[None]: with内で``bounded_planner``のPopenを置き換える]
# }
@contextmanager
def warm_worker_launch() -> Iterator[None]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [*sys.path, environment.get("PYTHONPATH", "")]
    )
    process = _REAL_POPEN(
        [sys.executable, "-m", "planning_warm_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        creationflags=(0x08000000 | 0x00000200) if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    handed_over = []
    try:
        marker = _read_marker(process)
        if marker != _WARM_MARKER:
            raise AssertionError("warm planning worker did not become ready")

        def launch(command, *args, **kwargs):
            if list(command[:3]) != _WORKER_COMMAND:
                return _REAL_POPEN(command, *args, **kwargs)
            if handed_over:
                raise AssertionError("warm planning worker is single-use")
            handed_over.append(True)
            process.stdin.write(json.dumps(list(command[3:])).encode("ascii") + b"\n")
            process.stdin.close()
            return process

        with patch("ai_game_player.bounded_planner.subprocess.Popen", launch):
            yield
    finally:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()


# {
#   責務: [_read_marker: 子processのimport完了行を上限時間内に読む]
#   引数: [process: warm worker] 戻り値: [bytes: 読めた行。期限切れはAssertionError]
# }
def _read_marker(process: subprocess.Popen[bytes]) -> bytes:
    lines: list[bytes] = []
    reader = threading.Thread(
        target=lambda: lines.append(process.stdout.readline()), daemon=True
    )
    reader.start()
    reader.join(_WARM_START_TIMEOUT_S)
    if not lines:
        raise AssertionError("warm planning worker start timed out")
    return lines[0]


# workerの``_execute``が資源制限の後にimportする製品module。factoryは含めない。
_PRELOADED_MODULES = (
    "ai_game_player.belief_contract",
    "ai_game_player.belief_validation",
    "ai_game_player.expectimax_search",
    "ai_game_player.planning_contract",
    "ai_game_player.planning_outcome",
    "ai_game_player.planning_result",
    "ai_game_player.planning_file_reader",
    "ai_game_player.planning_wire",
    "ai_game_player.world_state_hypothesis",
)


if __name__ == "__main__":
    import importlib

    from ai_game_player import planning_worker

    for name in _PRELOADED_MODULES:
        importlib.import_module(name)

    sys.stdout.buffer.write(_WARM_MARKER)
    sys.stdout.flush()
    arguments = json.loads(sys.stdin.buffer.readline())
    sys.exit(planning_worker.main(arguments))
