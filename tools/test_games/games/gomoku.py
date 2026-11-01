# SPDX-License-Identifier: GPL-3.0-or-later
"""Gomoku standalone game; rules preserved from the original suite."""

import random
import pygame
from ..drawing_boards import header, text


class Gomoku:
    """Freestyle Gomoku: five or more in a row wins; no forbidden moves."""

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.board = [[0] * 15 for _ in range(15)]
        self.turn = 1
        self.over = False
        self.status = "Your turn: black"
        self.wait = 0.35

    def won(self, x, y, side):
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            count = 1
            for sign in (-1, 1):
                cx, cy = x + sign * dx, y + sign * dy
                while 0 <= cx < 15 and 0 <= cy < 15 and self.board[cy][cx] == side:
                    count += 1
                    cx, cy = cx + sign * dx, cy + sign * dy
            if count >= 5:
                return True
        return False

    def play(self, x, y):
        if self.over or not (0 <= x < 15 and 0 <= y < 15) or self.board[y][x]:
            return False
        self.board[y][x] = self.turn
        if self.won(x, y, self.turn):
            self.over = True
            self.status = (
                "You win: five in a row"
                if self.turn == 1
                else "CPU wins: five in a row"
            )
        elif all(all(row) for row in self.board):
            self.over = True
            self.status = "Draw: board full"
        else:
            self.turn *= -1
            self.wait = 0.35
            self.status = "Your turn: black" if self.turn == 1 else "CPU turn: white"
        return True

    def handle(self, event):
        if (
            event.type == pygame.MOUSEBUTTONDOWN
            and event.button == 1
            and self.turn == 1
        ):
            self.play(round((event.pos[0] - 55) / 38), round((event.pos[1] - 120) / 38))

    def score(self, x, y, side):
        self.board[y][x] = side
        if self.won(x, y, side):
            self.board[y][x] = 0
            return 1000000
        value = 0
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            count, opens = 1, 0
            for sign in (-1, 1):
                cx, cy = x + sign * dx, y + sign * dy
                while 0 <= cx < 15 and 0 <= cy < 15 and self.board[cy][cx] == side:
                    count += 1
                    cx, cy = cx + sign * dx, cy + sign * dy
                if 0 <= cx < 15 and 0 <= cy < 15 and self.board[cy][cx] == 0:
                    opens += 1
            value += (10**count) * opens
        self.board[y][x] = 0
        return value

    def update(self, dt):
        if self.over or self.turn == 1:
            return
        self.wait -= dt
        if self.wait > 0:
            return
        moves = [(x, y) for y in range(15) for x in range(15) if not self.board[y][x]]
        self.rng.shuffle(moves)
        move = max(
            moves,
            key=lambda p: (
                max(self.score(*p, -1) * 1.1, self.score(*p, 1))
                - (abs(p[0] - 7) + abs(p[1] - 7))
            ),
        )
        self.play(*move)

    def draw(self, surface):
        header(surface, "Gomoku / 五目並べ", self.status)
        pygame.draw.rect(surface, (183, 143, 88), (36, 101, 570, 570))
        for i in range(15):
            pygame.draw.line(
                surface, (66, 47, 30), (55 + i * 38, 120), (55 + i * 38, 652)
            )
            pygame.draw.line(
                surface, (66, 47, 30), (55, 120 + i * 38), (587, 120 + i * 38)
            )
        for y in range(15):
            for x in range(15):
                if self.board[y][x]:
                    color = (23, 24, 29) if self.board[y][x] == 1 else (244, 241, 230)
                    pygame.draw.circle(surface, color, (55 + x * 38, 120 + y * 38), 16)
        text(surface, "You: black / CPU: white", 630, 130)
        text(surface, "Click an intersection.", 630, 175)
        text(surface, "Five or more in a row wins.", 630, 220, 19)
        text(surface, "Freestyle: no forbidden moves.", 630, 255, 18)
