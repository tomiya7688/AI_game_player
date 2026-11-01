# SPDX-License-Identifier: GPL-3.0-or-later
"""Human-playable test games; no connection to the player's internal state."""

import argparse
import os
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame

from .arcade import Breakout, Shooter, Tetris
from .boards import Chess, Gomoku, Othello, Shogi
from .session import ArcadeSession
from .worlds import FPS, Roguelike, Sandbox

GAMES = {
    "tetris": ("40 LINE Sprint", Tetris, "Clear 40 lines"),
    "othello": ("Othello vs CPU", Othello, "Black moves first"),
    "shogi": ("Shogi vs CPU", Shogi, "Move, promote and drop captured pieces"),
    "chess": ("Chess vs CPU", Chess, "White moves first"),
    "gomoku": ("Gomoku vs CPU", Gomoku, "Freestyle: five or more in a row"),
    "roguelike": ("Dungeon Escape", Roguelike, "Turn-based dungeon adventure"),
    "shooter": ("2D Space Shooter", Shooter, "Move, fire and survive"),
    "fps": ("First Person Arena", FPS, "Raycast first-person combat"),
    "sandbox": ("Block Sandbox", Sandbox, "Explore, mine and build"),
    "breakout": ("Breakout", Breakout, "Clear the bricks"),
}


def render_smoke(surface, output, seed):
    output.mkdir(parents=True, exist_ok=True)
    for key, (_, factory, _) in GAMES.items():
        instance = factory(seed=seed)
        instance.update(1 / 60)
        instance.draw(surface)
        pygame.image.save(surface, str(output / f"{key}.png"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", choices=GAMES)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--smoke",
        type=Path,
        help="Render every game to PNG and exit; no GUI input verification",
    )
    args = parser.parse_args(argv)
    pygame.init()
    try:
        pygame.display.set_caption("AI GAME PLAYER - Test Arcade | Tomiya")
        surface = pygame.display.set_mode((1000, 760))
        if args.smoke:
            render_smoke(surface, args.smoke, args.seed)
            return
        session = ArcadeSession(GAMES, seed=args.seed, current=args.game)
        clock = pygame.time.Clock()
        while session.running:
            dt = min(clock.tick(60) / 1000, 0.05)
            for event in pygame.event.get():
                session.handle(event)
                if not session.running:
                    break
            if session.running:
                session.update(dt)
                session.draw(surface)
                pygame.display.flip()
    finally:
        pygame.quit()


if __name__ == "__main__":
    main()
