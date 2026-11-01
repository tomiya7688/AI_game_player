# SPDX-License-Identifier: GPL-3.0-or-later
"""Breakout standalone game; rules preserved from the original suite."""

import math
import random
import pygame
from ..drawing_arcade import ACCENT, COLORS, INK, header, outcome, text


class Breakout:
    """One-board brick breaker with a launchable ball and three lives."""

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.paddle_x = 500.0
        self.ball = pygame.Vector2(500, 630)
        self.velocity = pygame.Vector2(0, 0)
        self.bricks = [
            pygame.Rect(66 + col * 87, 145 + row * 32, 80, 24)
            for row in range(6)
            for col in range(10)
        ]
        self.held = set()
        self.lives = 3
        self.score = 0
        self.launched = False
        self.status = "Playing"

    def handle(self, event):
        if event.type == pygame.KEYDOWN:
            self.held.add(event.key)
            if (
                event.key == pygame.K_SPACE
                and not self.launched
                and self.status == "Playing"
            ):
                self.launched = True
                self.velocity = pygame.Vector2(self.rng.choice((-170, 170)), -340)
        elif event.type == pygame.KEYUP:
            self.held.discard(event.key)
        elif event.type == pygame.MOUSEMOTION:
            self.paddle_x = max(112, min(888, event.pos[0]))
        elif event.type == pygame.WINDOWFOCUSLOST:
            self.held.clear()

    def update(self, dt):
        if self.status != "Playing":
            return
        dt = min(max(dt, 0), 0.1)
        steps = max(1, math.ceil(dt / 0.005))
        for _ in range(steps):
            if self.status != "Playing":
                break
            self._step(dt / steps)

    def _step(self, dt):
        dx = int(bool(self.held & {pygame.K_RIGHT, pygame.K_d})) - int(
            bool(self.held & {pygame.K_LEFT, pygame.K_a})
        )
        self.paddle_x = max(112, min(888, self.paddle_x + dx * 560 * dt))
        if not self.launched:
            self.ball.update(self.paddle_x, 630)
            return
        old = self.ball.copy()
        self.ball += self.velocity * dt
        if self.ball.x < 57:
            self.ball.x = 57
            self.velocity.x = abs(self.velocity.x)
        elif self.ball.x > 943:
            self.ball.x = 943
            self.velocity.x = -abs(self.velocity.x)
        if self.ball.y < 112:
            self.ball.y = 112
            self.velocity.y = abs(self.velocity.y)
        paddle = pygame.Rect(round(self.paddle_x - 62), 645, 124, 15)
        if (
            self.velocity.y > 0
            and old.y <= 638 <= self.ball.y
            and paddle.left - 6 <= self.ball.x <= paddle.right + 6
        ):
            offset = max(-1, min(1, (self.ball.x - self.paddle_x) / 62))
            speed = min(570, max(380, self.velocity.length() + 5))
            self.velocity = (
                pygame.Vector2(math.sin(offset * 1.08), -math.cos(offset * 1.08))
                * speed
            )
            self.ball.y = 637
        ball_rect = pygame.Rect(round(self.ball.x - 7), round(self.ball.y - 7), 14, 14)
        for brick in self.bricks:
            if brick.colliderect(ball_rect):
                self.bricks.remove(brick)
                self.score += 1
                if old.x + 7 <= brick.left or old.x - 7 >= brick.right:
                    self.velocity.x *= -1
                else:
                    self.velocity.y *= -1
                self.ball = old
                break
        if not self.bricks:
            self.status = "All bricks cleared!"
        elif self.ball.y > 710:
            self.lives -= 1
            self.launched = False
            self.ball.update(self.paddle_x, 630)
            self.velocity.update(0, 0)
            if self.lives <= 0:
                self.status = "Game over"

    def draw(self, surface):
        header(
            surface,
            "PRISM BREAKER",
            "Arrows / A,D / mouse: paddle     Space: launch     Clear all 60 bricks",
        )
        pygame.draw.rect(surface, (23, 34, 54), (48, 103, 904, 602), border_radius=10)
        for brick in self.bricks:
            row = (brick.y - 145) // 32
            pygame.draw.rect(surface, COLORS[row], brick, border_radius=4)
        pygame.draw.rect(
            surface, ACCENT, (round(self.paddle_x - 62), 645, 124, 15), border_radius=7
        )
        pygame.draw.circle(surface, INK, (round(self.ball.x), round(self.ball.y)), 7)
        text(surface, f"LIVES {self.lives}     BRICKS {self.score}/60", (65, 675), 25)
        if not self.launched and self.status == "Playing":
            text(surface, "Press Space to launch", (379, 550), 29, ACCENT)
        outcome(surface, self.status)
