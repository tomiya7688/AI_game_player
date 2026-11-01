"""Human-playable test games; no connection to the player's internal state."""

import argparse
import os
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame

from .arcade import Breakout, Shooter, Tetris
from .boards import Chess, Gomoku, Othello, Shogi
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


def menu(surface, selected):
    surface.fill((15, 22, 37))
    font = pygame.font.SysFont("Segoe UI", 23)
    title = pygame.font.SysFont("Segoe UI", 39, bold=True)
    surface.blit(title.render("AI GAME PLAYER / TEST ARCADE", True, "white"), (40, 22))
    surface.blit(
        font.render("Choose a game - mouse or arrows + Enter", True, (153, 181, 207)),
        (42, 76),
    )
    boxes = []
    for index, (key, (name, _, description)) in enumerate(GAMES.items()):
        rect = pygame.Rect(40 + (index % 2) * 480, 130 + (index // 2) * 108, 460, 92)
        pygame.draw.rect(
            surface,
            (36, 72, 105) if index == selected else (26, 38, 57),
            rect,
            border_radius=12,
        )
        surface.blit(
            font.render(f"{index + 1:02}  {name}", True, (123, 225, 201)),
            (rect.x + 18, rect.y + 14),
        )
        surface.blit(
            pygame.font.SysFont("Segoe UI", 18).render(description, True, "white"),
            (rect.x + 18, rect.y + 51),
        )
        boxes.append((rect, key))
    return boxes


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
    pygame.display.set_caption("AI GAME PLAYER - Test Arcade | Tomiya")
    surface = pygame.display.set_mode((1000, 760))
    clock = pygame.time.Clock()
    footer = pygame.font.SysFont("Segoe UI", 19)
    current = args.game
    game = GAMES[current][1](seed=args.seed) if current else None
    selected = 0
    paused = False
    if args.smoke:
        args.smoke.mkdir(parents=True, exist_ok=True)
        for key, (_, factory, _) in GAMES.items():
            instance = factory(seed=args.seed)
            instance.update(1 / 60)
            instance.draw(surface)
            pygame.image.save(surface, str(args.smoke / f"{key}.png"))
        pygame.quit()
        return
    running = True
    while running:
        dt = min(clock.tick(60) / 1000, 0.05)
        boxes = menu(surface, selected) if game is None else []
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.WINDOWFOCUSLOST and game:
                paused = True
                game.handle(event)
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                if game:
                    game = None
                    current = None
                    paused = False
                else:
                    running = False
            elif game and event.type == pygame.KEYDOWN and event.key == pygame.K_r:
                game = GAMES[current][1](seed=args.seed)
                paused = False
            elif game and event.type == pygame.KEYDOWN and event.key == pygame.K_p:
                paused = not paused
                if paused:
                    game.handle(pygame.event.Event(pygame.WINDOWFOCUSLOST))
            elif game and (not paused or event.type == pygame.KEYUP):
                game.handle(event)
            elif game is None:
                if event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_DOWN, pygame.K_RIGHT):
                        selected = (selected + 1) % len(GAMES)
                    elif event.key in (pygame.K_UP, pygame.K_LEFT):
                        selected = (selected - 1) % len(GAMES)
                    elif event.key == pygame.K_RETURN:
                        current = list(GAMES)[selected]
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    current = next(
                        (key for rect, key in boxes if rect.collidepoint(event.pos)),
                        None,
                    )
                if current:
                    game = GAMES[current][1](seed=args.seed)
        if game:
            if not paused:
                game.update(dt)
            game.draw(surface)
            if paused:
                shade = pygame.Surface((1000, 720), pygame.SRCALPHA)
                shade.fill((0, 0, 0, 180))
                surface.blit(shade, (0, 0))
                surface.blit(
                    footer.render("PAUSED - press P to resume", True, "white"),
                    (350, 350),
                )
        pygame.draw.rect(surface, (10, 16, 28), (0, 720, 1000, 40))
        surface.blit(
            footer.render(
                "R restart   |   P pause   |   Esc menu / quit   |   Tomiya",
                True,
                (183, 204, 222),
            ),
            (24, 729),
        )
        pygame.display.flip()
    pygame.quit()


if __name__ == "__main__":
    main()
