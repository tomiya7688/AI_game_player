# SPDX-License-Identifier: GPL-3.0-or-later
"""Menu event integration tests; these do not verify native OS input."""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame
import pytest

from tools.test_games import __main__ as launcher


@pytest.mark.parametrize(
    "event",
    [
        pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN),
        pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(100, 160)),
    ],
)
def test_menu_starts_selected_game(monkeypatch, event):
    constructed = []

    class Game:
        def __init__(self, seed=0):
            constructed.append(seed)

        def handle(self, event):
            pass

        def update(self, dt):
            pass

        def draw(self, surface):
            surface.fill("black")

    monkeypatch.setattr(launcher, "GAMES", {"example": ("Example", Game, "Test")})
    monkeypatch.setattr(
        pygame.event,
        "get",
        lambda: [event, pygame.event.Event(pygame.QUIT)],
    )
    launcher.main(["--seed", "12"])
    assert constructed == [12]
