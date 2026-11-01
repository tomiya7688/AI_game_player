# SPDX-License-Identifier: GPL-3.0-or-later
"""Othello standalone game; rules preserved from the original suite."""

import random
import pygame
from ..drawing_boards import header, text


class Othello:
    """Eight-by-eight Othello; automatic passes and final disk counts."""

    origin = (40, 115)
    cell = 64

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.board = [[0] * 8 for _ in range(8)]
        self.board[3][3] = self.board[4][4] = -1
        self.board[3][4] = self.board[4][3] = 1
        self.turn = 1
        self.over = False
        self.status = "Your turn: black"
        self.wait = 0.35

    def flips(self, x, y, side):
        if not (0 <= x < 8 and 0 <= y < 8) or self.board[y][x]:
            return []
        result = []
        for dx, dy in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),
        ):
            cx, cy = x + dx, y + dy
            line = []
            while 0 <= cx < 8 and 0 <= cy < 8 and self.board[cy][cx] == -side:
                line.append((cx, cy))
                cx, cy = cx + dx, cy + dy
            if line and 0 <= cx < 8 and 0 <= cy < 8 and self.board[cy][cx] == side:
                result.extend(line)
        return result

    def legal(self, side):
        return [(x, y) for y in range(8) for x in range(8) if self.flips(x, y, side)]

    def play(self, x, y):
        captured = self.flips(x, y, self.turn)
        if not captured or self.over:
            return False
        for cx, cy in captured + [(x, y)]:
            self.board[cy][cx] = self.turn
        self.turn *= -1
        self.wait = 0.35
        passed = ""
        if not self.legal(self.turn):
            self.turn *= -1
            passed = "Opponent passes. "
            if not self.legal(self.turn):
                self.over = True
                black = sum(row.count(1) for row in self.board)
                white = sum(row.count(-1) for row in self.board)
                result = (
                    "You win"
                    if black > white
                    else "CPU wins"
                    if white > black
                    else "Draw"
                )
                self.status = f"{result}: black {black}, white {white}"
                return True
        self.status = passed + (
            "Your turn: black" if self.turn == 1 else "CPU turn: white"
        )
        return True

    def handle(self, event):
        if (
            event.type == pygame.MOUSEBUTTONDOWN
            and event.button == 1
            and self.turn == 1
        ):
            x = (event.pos[0] - self.origin[0]) // self.cell
            y = (event.pos[1] - self.origin[1]) // self.cell
            self.play(x, y)

    def update(self, dt):
        if self.over or self.turn == 1:
            return
        self.wait -= dt
        if self.wait <= 0:
            moves = self.legal(-1)
            self.rng.shuffle(moves)
            move = max(
                moves,
                key=lambda p: (
                    len(self.flips(*p, -1))
                    + (30 if p[0] in (0, 7) and p[1] in (0, 7) else 0)
                ),
            )
            self.play(*move)

    def draw(self, surface):
        header(surface, "Othello / オセロ", self.status)
        legal = self.legal(1) if self.turn == 1 and not self.over else []
        for y in range(8):
            for x in range(8):
                rect = pygame.Rect(40 + x * 64, 115 + y * 64, 64, 64)
                pygame.draw.rect(surface, (27, 121, 86), rect)
                pygame.draw.rect(surface, (15, 63, 48), rect, 1)
                if self.board[y][x]:
                    color = (20, 22, 28) if self.board[y][x] == 1 else (242, 240, 226)
                    pygame.draw.circle(surface, color, rect.center, 26)
                elif (x, y) in legal:
                    pygame.draw.circle(surface, (111, 200, 151), rect.center, 6)
        text(surface, "You: black / CPU: white", 590, 130)
        text(surface, "Click a highlighted square.", 590, 175)
        text(surface, "No legal move: automatic pass.", 590, 215, 19)
        text(surface, "Most disks at the end wins.", 590, 250, 19)
