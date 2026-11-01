# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only test oracle around the normal launcher; never used by the AI."""

import argparse
import hashlib
import json
import os
import pickle
from pathlib import Path

import pygame

from tools.test_games import __main__ as launcher
from tools.test_games.session import ArcadeSession


def digest(game):
    return hashlib.sha256(pickle.dumps(game)).hexdigest()


def observe(game):
    result = {}
    if game is None:
        return result
    for name in ("x", "y", "angle", "selected", "turns", "launched", "paddle_x"):
        value = getattr(game, name, None)
        if value is None or isinstance(value, (int, float, bool, str)):
            result[name] = value
    result["shots"] = len(getattr(game, "shots", []))
    board = getattr(game, "board", None)
    if isinstance(board, list):
        result["board"] = board
    elif board is not None and hasattr(board, "move_stack"):
        result["moves"] = [str(move) for move in board.move_stack]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=launcher.GAMES, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    args = parser.parse_args()

    class ProbeSession(ArcadeSession):
        def __init__(self, *arguments, **kwargs):
            super().__init__(*arguments, **kwargs)
            self.frames = 0
            self.initial_hash = digest(self.game)
            self.restarts = []
            self.events = []

        def handle(self, event):
            super().handle(event)
            if event.type in (pygame.KEYDOWN, pygame.KEYUP):
                self.events.append(
                    [pygame.event.event_name(event.type), pygame.key.name(event.key)]
                )
                if event.type == pygame.KEYDOWN and event.key == pygame.K_r:
                    self.restarts.append(digest(self.game))
            elif event.type == pygame.MOUSEBUTTONDOWN:
                self.events.append(["MouseButtonDown", list(event.pos)])

        def draw(self, surface):
            super().draw(surface)
            self.frames += 1
            value = {
                "pid": os.getpid(),
                "frames": self.frames,
                "current": self.current,
                "paused": self.paused,
                "initial_hash": self.initial_hash,
                "restarts": self.restarts,
                "state_hash": digest(self.game),
                "game": observe(self.game),
                "events": self.events,
                "evidence_kind": "read-only test oracle, not AI observation",
            }
            temporary = args.oracle.with_suffix(".tmp")
            temporary.write_text(json.dumps(value))
            temporary.replace(args.oracle)

    launcher.ArcadeSession = ProbeSession
    launcher.main(["--game", args.game, "--seed", str(args.seed)])


if __name__ == "__main__":
    main()
