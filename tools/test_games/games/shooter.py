# SPDX-License-Identifier: GPL-3.0-or-later
"""Shooter standalone game; rules preserved from the original suite."""

import math
import random
import pygame
from ..drawing_arcade import ACCENT, BG, header, outcome, text


class Shooter:
    """Three-wave vertical shooter with continuous movement and visible health."""

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.player = pygame.Vector2(500, 635)
        self.held = set()
        self.shots = []
        self.enemy_shots = []
        self.enemies = []
        self.lives = 3
        self.kills = 0
        self.wave = 0
        self.cooldown = 0.0
        self.enemy_cooldown = 0.0
        self.invincible = 0.0
        self.elapsed = 0.0
        self.status = "Playing"
        self._wave()

    def _wave(self):
        self.wave += 1
        self.enemies = [
            [pygame.Vector2(190 + col * 100, 135 + row * 60), 0.0]
            for row in range(2 + self.wave)
            for col in range(7)
        ]

    def handle(self, event):
        if event.type == pygame.KEYDOWN:
            self.held.add(event.key)
        elif event.type == pygame.KEYUP:
            self.held.discard(event.key)
        elif event.type == pygame.WINDOWFOCUSLOST:
            self.held.clear()

    def update(self, dt):
        if self.status != "Playing":
            return
        dt = min(max(dt, 0), 0.1)
        # Small steps keep fast projectiles from skipping enemy hit boxes.
        steps = max(1, math.ceil(dt / 0.01))
        for _ in range(steps):
            if self.status != "Playing":
                break
            self._step(dt / steps)

    def _step(self, dt):
        self.elapsed += dt
        dx = int(bool(self.held & {pygame.K_RIGHT, pygame.K_d})) - int(
            bool(self.held & {pygame.K_LEFT, pygame.K_a})
        )
        dy = int(bool(self.held & {pygame.K_DOWN, pygame.K_s})) - int(
            bool(self.held & {pygame.K_UP, pygame.K_w})
        )
        movement = pygame.Vector2(dx, dy)
        if movement.length_squared():
            self.player += movement.normalize() * 340 * dt
        self.player.x = max(65, min(935, self.player.x))
        self.player.y = max(420, min(675, self.player.y))
        self.cooldown -= dt
        self.invincible = max(0.0, self.invincible - dt)
        if pygame.K_SPACE in self.held and self.cooldown <= 0:
            self.shots.append(self.player.copy() - pygame.Vector2(0, 23))
            self.cooldown = 0.18
        for shot in self.shots:
            shot.y -= 650 * dt
        self.shots = [shot for shot in self.shots if shot.y > 95]
        for enemy in self.enemies:
            enemy[0].x += math.sin(self.elapsed * 1.4) * 36 * dt
            enemy[0].y += (5 + self.wave * 2) * dt
        for shot in self.shots[:]:
            for enemy in self.enemies[:]:
                if abs(shot.x - enemy[0].x) < 24 and abs(shot.y - enemy[0].y) < 18:
                    self.shots.remove(shot)
                    self.enemies.remove(enemy)
                    self.kills += 1
                    break
        self.enemy_cooldown -= dt
        if self.enemies and self.enemy_cooldown <= 0:
            enemy = self.rng.choice(self.enemies)
            self.enemy_shots.append(enemy[0].copy())
            self.enemy_cooldown = max(0.22, 0.9 - 0.18 * self.wave)
        for shot in self.enemy_shots:
            shot.y += (220 + 35 * self.wave) * dt
            if (
                self.invincible == 0
                and abs(shot.x - self.player.x) < 21
                and abs(shot.y - self.player.y) < 22
            ):
                self.lives -= 1
                self.invincible = 1.4
                shot.y = 800
        self.enemy_shots = [shot for shot in self.enemy_shots if shot.y < 705]
        if self.lives <= 0 or any(enemy[0].y > 415 for enemy in self.enemies):
            self.status = "Game over"
        elif not self.enemies:
            if self.wave == 3:
                self.status = "All 3 waves cleared!"
            else:
                self.enemy_shots.clear()
                self._wave()

    def draw(self, surface):
        header(
            surface,
            "ORBIT DEFENDER",
            "Arrows / WASD: move     Hold Space: fire     Clear all 3 waves",
        )
        pygame.draw.rect(surface, (22, 33, 54), (40, 103, 920, 599), border_radius=12)
        for i in range(65):
            x = 50 + (i * 157) % 900
            y = 110 + (i * 97 + int(self.elapsed * (12 + i % 7))) % 580
            pygame.draw.circle(surface, (66, 85, 112), (x, y), 1)
        pygame.draw.line(surface, (78, 45, 62), (50, 415), (950, 415), 1)
        for enemy, _ in self.enemies:
            pygame.draw.rect(
                surface,
                (244, 116, 140),
                (enemy.x - 22, enemy.y - 14, 44, 28),
                border_radius=7,
            )
            pygame.draw.circle(surface, BG, (int(enemy.x - 8), int(enemy.y)), 4)
            pygame.draw.circle(surface, BG, (int(enemy.x + 8), int(enemy.y)), 4)
        for shot in self.shots:
            pygame.draw.line(surface, ACCENT, shot, shot + pygame.Vector2(0, 13), 4)
        for shot in self.enemy_shots:
            pygame.draw.circle(
                surface, (250, 187, 87), (round(shot.x), round(shot.y)), 5
            )
        if self.invincible == 0 or int(self.elapsed * 12) % 2 == 0:
            x, y = self.player
            pygame.draw.polygon(
                surface,
                ACCENT,
                ((x, y - 22), (x - 20, y + 18), (x, y + 9), (x + 20, y + 18)),
            )
        text(
            surface,
            f"WAVE {self.wave}/3     LIVES {self.lives}     HITS {self.kills}",
            (58, 672),
            25,
        )
        outcome(surface, self.status)
