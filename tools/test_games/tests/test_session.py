# SPDX-License-Identifier: GPL-3.0-or-later
"""Lifecycle contracts for all games; event injection is not OS-input evidence."""

import os
import pickle

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame
import pytest

from tools.test_games import __main__ as launcher
from tools.test_games.session import ArcadeSession


@pytest.fixture(autouse=True)
def pygame_lifecycle():
    pygame.init()
    yield
    pygame.quit()


def key(value):
    return pygame.event.Event(pygame.KEYDOWN, key=value)


@pytest.mark.parametrize("name", launcher.GAMES)
def test_pause_restart_seed_and_return_to_menu_for_every_game(name):
    session = ArcadeSession(launcher.GAMES, seed=29, current=name)
    initial = pickle.dumps(session.game)
    session.update(0.04)
    session.handle(key(pygame.K_p))
    assert session.paused
    paused = pickle.dumps(session.game)
    session.update(2.0)
    session.handle(key(pygame.K_SPACE))
    assert pickle.dumps(session.game) == paused
    session.handle(key(pygame.K_p))
    assert not session.paused
    session.handle(key(pygame.K_r))
    assert pickle.dumps(session.game) == initial
    session.handle(key(pygame.K_ESCAPE))
    assert session.game is None
    assert session.current is None
    assert session.running
    session.handle(key(pygame.K_ESCAPE))
    assert not session.running


@pytest.mark.parametrize("name", ("tetris", "shooter", "fps", "sandbox", "breakout"))
def test_focus_loss_pauses_and_releases_held_controls(name):
    session = ArcadeSession(launcher.GAMES, current=name)
    session.handle(key(pygame.K_LEFT))
    session.handle(pygame.event.Event(pygame.WINDOWFOCUSLOST))
    assert session.paused
    held = getattr(session.game, "held", getattr(session.game, "keys", set()))
    assert not held
    session.handle(key(pygame.K_p))
    assert not session.paused
    assert not held


def test_smoke_save_failure_still_quits_pygame(monkeypatch, tmp_path):
    def fail_save(*args):
        raise OSError("fixture output is unavailable")

    monkeypatch.setattr(pygame.image, "save", fail_save)
    with pytest.raises(OSError, match="fixture output"):
        launcher.main(["--smoke", str(tmp_path)])
    assert not pygame.get_init()


def test_window_startup_failure_still_quits_pygame(monkeypatch):
    def fail_window(*args):
        raise pygame.error("display is unavailable")

    monkeypatch.setattr(pygame.display, "set_mode", fail_window)
    with pytest.raises(pygame.error, match="display is unavailable"):
        launcher.main(["--game", "tetris"])
    assert not pygame.get_init()
