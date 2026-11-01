# SPDX-License-Identifier: GPL-3.0-or-later
"""Own the standalone arcade lifecycle, without importing the AI player."""

import pygame


def menu_entries(games):
    return [
        (pygame.Rect(40 + (i % 2) * 480, 130 + (i // 2) * 108, 460, 92), key)
        for i, key in enumerate(games)
    ]


class ArcadeSession:
    """Route visible menu/game input and pause, restart, and quit transitions."""

    def __init__(self, games, seed=0, current=None):
        self.games = games
        self.seed = seed
        self.current = current
        self.game = games[current][1](seed=seed) if current else None
        self.selected = 0
        self.paused = False
        self.running = True
        self.footer = pygame.font.SysFont("Segoe UI", 19)
        self.font = pygame.font.SysFont("Segoe UI", 23)
        self.title = pygame.font.SysFont("Segoe UI", 39, bold=True)
        self.description = pygame.font.SysFont("Segoe UI", 18)

    def handle(self, event):
        if event.type == pygame.QUIT:
            self.running = False
        elif event.type == pygame.WINDOWFOCUSLOST and self.game is not None:
            self.paused = True
            self.game.handle(event)
        elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            if self.game is None:
                self.running = False
            else:
                self.game = None
                self.current = None
                self.paused = False
        elif self.game is not None:
            if event.type == pygame.KEYDOWN and event.key == pygame.K_r:
                self.game = self.games[self.current][1](seed=self.seed)
                self.paused = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_p:
                self.paused = not self.paused
                if self.paused:
                    self.game.handle(pygame.event.Event(pygame.WINDOWFOCUSLOST))
            elif not self.paused or event.type == pygame.KEYUP:
                self.game.handle(event)
        else:
            self._select_menu_game(event)

    def _select_menu_game(self, event):
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_DOWN, pygame.K_RIGHT):
                self.selected = (self.selected + 1) % len(self.games)
            elif event.key in (pygame.K_UP, pygame.K_LEFT):
                self.selected = (self.selected - 1) % len(self.games)
            elif event.key == pygame.K_RETURN:
                self.current = list(self.games)[self.selected]
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self.current = next(
                (
                    key
                    for rect, key in menu_entries(self.games)
                    if rect.collidepoint(event.pos)
                ),
                None,
            )
        if self.current:
            self.game = self.games[self.current][1](seed=self.seed)

    def update(self, dt):
        if self.running and self.game is not None and not self.paused:
            self.game.update(dt)

    def draw(self, surface):
        if self.game is None:
            self._draw_menu(surface)
        else:
            self.game.draw(surface)
            if self.paused:
                shade = pygame.Surface((1000, 720), pygame.SRCALPHA)
                shade.fill((0, 0, 0, 180))
                surface.blit(shade, (0, 0))
                surface.blit(
                    self.footer.render("PAUSED - press P to resume", True, "white"),
                    (350, 350),
                )
        pygame.draw.rect(surface, (10, 16, 28), (0, 720, 1000, 40))
        surface.blit(
            self.footer.render(
                "R restart   |   P pause   |   Esc menu / quit   |   Tomiya",
                True,
                (183, 204, 222),
            ),
            (24, 729),
        )

    def _draw_menu(self, surface):
        surface.fill((15, 22, 37))
        surface.blit(
            self.title.render("AI GAME PLAYER / TEST ARCADE", True, "white"), (40, 22)
        )
        surface.blit(
            self.font.render(
                "Choose a game - mouse or arrows + Enter", True, (153, 181, 207)
            ),
            (42, 76),
        )
        for index, (rect, key) in enumerate(menu_entries(self.games)):
            name, _, description = self.games[key]
            pygame.draw.rect(
                surface,
                (36, 72, 105) if index == self.selected else (26, 38, 57),
                rect,
                border_radius=12,
            )
            surface.blit(
                self.font.render(f"{index + 1:02}  {name}", True, (123, 225, 201)),
                (rect.x + 18, rect.y + 14),
            )
            surface.blit(
                self.description.render(description, True, "white"),
                (rect.x + 18, rect.y + 51),
            )
