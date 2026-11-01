# SPDX-License-Identifier: GPL-3.0-or-later
"""Tetris standalone game; rules preserved from the original suite."""

import random
import pygame
from ..drawing_arcade import ACCENT, COLORS, MUTED, header, outcome, text


class Tetris:
    """Seven-bag falling blocks with a 40-line sprint and clockwise wall kicks."""

    SHAPES = (
        ((0, 1), (1, 1), (2, 1), (3, 1)),
        ((1, 0), (2, 0), (1, 1), (2, 1)),
        ((1, 0), (0, 1), (1, 1), (2, 1)),
        ((1, 0), (2, 0), (0, 1), (1, 1)),
        ((0, 0), (1, 0), (1, 1), (2, 1)),
        ((0, 0), (0, 1), (1, 1), (2, 1)),
        ((2, 0), (0, 1), (1, 1), (2, 1)),
    )

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.board = [[0] * 10 for _ in range(20)]
        self.bag = []
        self.next_kind = self._take()
        self.lines = 0
        self.elapsed = 0.0
        self.fall = 0.0
        self.repeat = 0.0
        self.held = set()
        self.status = "Playing"
        self._spawn()

    def _take(self):
        if not self.bag:
            self.bag = list(range(7))
            self.rng.shuffle(self.bag)
        return self.bag.pop()

    def _spawn(self):
        self.kind = self.next_kind
        self.next_kind = self._take()
        self.cells = list(self.SHAPES[self.kind])
        self.x, self.y = 3, 0
        self.fall = 0.0
        if not self._fits(self.cells, self.x, self.y):
            self.status = "Game over"

    def _fits(self, cells, x, y):
        return all(
            0 <= x + dx < 10 and 0 <= y + dy < 20 and not self.board[y + dy][x + dx]
            for dx, dy in cells
        )

    def _move(self, dx):
        if self._fits(self.cells, self.x + dx, self.y):
            self.x += dx

    def _rotate(self):
        if self.kind == 1:
            return
        # I rotates in its 4x4 box; the other pieces rotate around cell (1,1).
        size = 4 if self.kind == 0 else 3
        rotated = [(size - 1 - y, x) for x, y in self.cells]
        for dx, dy in ((0, 0), (-1, 0), (1, 0), (-2, 0), (2, 0), (0, -1), (0, -2)):
            if self._fits(rotated, self.x + dx, self.y + dy):
                self.cells = rotated
                self.x += dx
                self.y += dy
                break

    def _lock(self):
        for dx, dy in self.cells:
            self.board[self.y + dy][self.x + dx] = self.kind + 1
        remaining = [row for row in self.board if not all(row)]
        cleared = 20 - len(remaining)
        self.lines += cleared
        self.board = [[0] * 10 for _ in range(cleared)] + remaining
        if self.lines >= 40:
            self.status = "40 lines cleared!"
        else:
            self._spawn()

    def _down(self):
        if self._fits(self.cells, self.x, self.y + 1):
            self.y += 1
        else:
            self._lock()

    def handle(self, event):
        if event.type == pygame.KEYUP:
            self.held.discard(event.key)
        if event.type == pygame.WINDOWFOCUSLOST:
            self.held.clear()
        if event.type != pygame.KEYDOWN or self.status != "Playing":
            return
        # Reject operating-system key repeats; movement repeats use simulation time.
        if event.key in self.held:
            return
        self.held.add(event.key)
        if event.key == pygame.K_LEFT:
            self._move(-1)
            self.repeat = -0.15
        elif event.key == pygame.K_RIGHT:
            self._move(1)
            self.repeat = -0.15
        elif event.key in (pygame.K_UP, pygame.K_x):
            self._rotate()
        elif event.key == pygame.K_SPACE:
            while self._fits(self.cells, self.x, self.y + 1):
                self.y += 1
            self._lock()

    def update(self, dt):
        if self.status != "Playing":
            return
        dt = min(max(dt, 0), 0.1)
        self.elapsed += dt
        direction = int(pygame.K_RIGHT in self.held) - int(pygame.K_LEFT in self.held)
        if direction:
            self.repeat += dt
            if self.repeat >= 0.07:
                self._move(direction)
                self.repeat = 0.0
        self.fall += dt
        interval = 0.045 if pygame.K_DOWN in self.held else 0.65
        if self.fall >= interval:
            self.fall = 0.0
            self._down()

    def draw(self, surface):
        header(
            surface,
            "40 LINE SPRINT",
            "Clear 40 lines. A seeded seven-bag supplies all seven pieces.",
        )
        origin_x, origin_y, cell = 310, 108, 28
        pygame.draw.rect(
            surface,
            (27, 39, 59),
            (origin_x - 3, origin_y - 3, 286, 566),
            border_radius=5,
        )
        ghost_y = self.y
        while self._fits(self.cells, self.x, ghost_y + 1):
            ghost_y += 1
        for y, row in enumerate(self.board):
            for x, value in enumerate(row):
                rect = pygame.Rect(
                    origin_x + x * cell, origin_y + y * cell, cell - 1, cell - 1
                )
                pygame.draw.rect(
                    surface,
                    COLORS[value - 1] if value else (19, 29, 46),
                    rect,
                    border_radius=3,
                )
        if self.status == "Playing":
            for dx, dy in self.cells:
                pygame.draw.rect(
                    surface,
                    MUTED,
                    (
                        origin_x + (self.x + dx) * cell,
                        origin_y + (ghost_y + dy) * cell,
                        cell - 1,
                        cell - 1,
                    ),
                    1,
                )
                pygame.draw.rect(
                    surface,
                    COLORS[self.kind],
                    (
                        origin_x + (self.x + dx) * cell,
                        origin_y + (self.y + dy) * cell,
                        cell - 1,
                        cell - 1,
                    ),
                    border_radius=3,
                )
        text(surface, f"LINES  {self.lines} / 40", (640, 125), 32, ACCENT)
        text(surface, f"TIME   {self.elapsed:05.1f}s", (640, 170), 29)
        text(surface, "NEXT", (660, 240), 25, MUTED)
        for x, y in self.SHAPES[self.next_kind]:
            pygame.draw.rect(
                surface,
                COLORS[self.next_kind],
                (660 + x * 25, 280 + y * 25, 23, 23),
                border_radius=3,
            )
        for i, line in enumerate(
            (
                "Left / Right: move",
                "Up / X: rotate",
                "Down: soft drop",
                "Space: hard drop",
            )
        ):
            text(surface, line, (640, 410 + 34 * i), 24)
        outcome(surface, self.status)
