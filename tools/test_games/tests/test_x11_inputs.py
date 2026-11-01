# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional real X11 input: owned window, gameplay, pause, restart, and menu."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.test_games import __main__ as launcher
from tools.test_games.tests.x11_driver import X11Driver

DISPLAY = os.environ.get("TEST_GAMES_X11_DISPLAY")
pytestmark = pytest.mark.skipif(
    not DISPLAY or sys.platform != "linux",
    reason="explicit isolated X11 display required; SDL event tests are separate",
)


def eventually(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("owned game did not reach the expected input state")


def game_effect(name, value):
    game = value["game"]
    if name == "tetris":
        return game["x"] < 3
    if name in ("othello", "gomoku"):
        x, y = (3, 2) if name == "othello" else (7, 7)
        return game["board"][y][x] == 1
    if name in ("chess", "shogi"):
        moves = game.get("moves", [])
        return bool(moves) and moves[0] == ("e2e4" if name == "chess" else "7g7f")
    if name == "fps":
        return game["angle"] > 0.1
    if name == "sandbox":
        return game["selected"] == 2
    field = {"roguelike": "turns", "shooter": "shots", "breakout": "launched"}[name]
    return game[field] > 0


@pytest.mark.parametrize("name", launcher.GAMES)
def test_actual_x11_game_controls(name, tmp_path):
    evidence = os.environ.get("TEST_GAMES_X11_EVIDENCE_DIR")
    output = Path(evidence) / name if evidence else tmp_path / name
    output.mkdir(parents=True, exist_ok=False)
    oracle = output / "oracle.json"
    environment = dict(
        os.environ,
        DISPLAY=DISPLAY,
        SDL_VIDEODRIVER="x11",
        SDL_AUDIODRIVER="dummy",
        XDG_CACHE_HOME=str((output / "cache").resolve()),
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tools.test_games.tests.gui_probe",
            "--game",
            name,
            "--seed",
            "29",
            "--oracle",
            str(oracle),
        ],
        env=environment,
    )
    driver = None

    def read():
        return json.loads(oracle.read_text()) if oracle.exists() else {}

    def tap(key):
        driver.key(key, True)
        time.sleep(0.07)
        driver.key(key, False)

    try:
        driver = X11Driver(DISPLAY)
        eventually(lambda: driver.find_owned_window(process.pid))
        driver.focus()
        eventually(lambda: read().get("frames", 0) >= 3)
        if read()["paused"]:
            tap("p")
        eventually(lambda: not read()["paused"])
        driver.capture(output / "before.png")
        if name == "tetris":
            tap("Left")
        elif name in ("othello", "gomoku"):
            driver.click(*((264, 275) if name == "othello" else (321, 386)))
        elif name in ("chess", "shogi"):
            start, end = (
                ((328, 531), (328, 403))
                if name == "chess"
                else ((190, 505), (190, 445))
            )
            driver.click(*start)
            time.sleep(0.1)
            driver.click(*end)
        elif name == "fps":
            driver.key("Right", True)
            time.sleep(0.15)
            driver.key("Right", False)
        elif name == "sandbox":
            tap("2")
        else:
            tap("space")
        effect = eventually(lambda: read() if game_effect(name, read()) else None)
        (output / "after-input.json").write_text(json.dumps(effect, indent=2))
        driver.capture(output / "after-input.png")
        tap("p")
        paused = eventually(lambda: read() if read().get("paused") else None)
        time.sleep(0.15)
        tap("space")
        later = read()
        assert later["paused"] and later["frames"] > paused["frames"]
        assert later["state_hash"] == paused["state_hash"]
        tap("p")
        eventually(lambda: not read()["paused"])
        tap("r")
        restarted = eventually(lambda: read() if read().get("restarts") else None)
        assert restarted["restarts"][-1] == restarted["initial_hash"]
        tap("Escape")
        eventually(lambda: read().get("current") is None)
        driver.capture(output / "menu.png")
        tap("Escape")
        assert process.wait(timeout=5) == 0
        (output / "result.json").write_text(
            json.dumps(
                {
                    "game": name,
                    "pid": process.pid,
                    "display": DISPLAY,
                    "verification": [
                        "owned X11 window",
                        "XTest OS input",
                        "game effect",
                        "pause blocks input/update",
                        "restart preserves seed",
                        "return menu and exit",
                    ],
                    "ai_autonomous_play": "unverified",
                    "pause": {
                        "before_hash": paused["state_hash"],
                        "after_hash": later["state_hash"],
                        "before_frames": paused["frames"],
                        "after_frames": later["frames"],
                    },
                    "restart": {
                        "initial_hash": restarted["initial_hash"],
                        "restart_hash": restarted["restarts"][-1],
                    },
                },
                indent=2,
            )
        )
    finally:
        if driver is not None:
            driver.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
