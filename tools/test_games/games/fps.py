# SPDX-License-Identifier: GPL-3.0-or-later
"""FPS standalone game; rules preserved from the original suite."""

import math
import random
import pygame
from ..drawing_worlds import heading, text


class FPS:
    """Ray-cast first-person arena with wall-clipped sprites and hitscan shots."""

    MAP = (
        "################",
        "#..............#",
        "#..##...##.....#",
        "#..##..........#",
        "#.......#......#",
        "#.......#..##..#",
        "#..##......##..#",
        "#..##..........#",
        "#..............#",
        "#......##......#",
        "#..............#",
        "################",
    )

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.x, self.y, self.angle = 2.5, 1.5, 0.0
        self.enemies = [[12.5, 2.5, 3], [8.5, 7.5, 3], [13.5, 9.5, 3], [3.5, 9.5, 3]]
        self.hp = 100
        self.keys = set()
        self.cooldown = 0.0
        self.attack_clock = 0.0
        self.flash = 0.0
        self.finished = False
        self.status = "Defeat all four sentries. Walls stop shots."

    def open(self, x, y):
        return (
            0 <= y < len(self.MAP)
            and 0 <= x < len(self.MAP[0])
            and self.MAP[int(y)][int(x)] != "#"
        )

    def can_stand(self, x, y):
        return all(
            self.open(x + dx, y + dy) for dx in (-0.18, 0.18) for dy in (-0.18, 0.18)
        )

    def ray(self, angle, maximum=24):
        distance = 0.02
        while distance < maximum:
            if not self.open(
                self.x + math.cos(angle) * distance, self.y + math.sin(angle) * distance
            ):
                return distance
            distance += 0.035
        return maximum

    def visible(self, x, y):
        distance = math.hypot(x - self.x, y - self.y)
        return self.ray(math.atan2(y - self.y, x - self.x), distance) >= distance

    def shoot(self):
        if self.finished or self.cooldown > 0:
            return
        self.cooldown = 0.25
        self.flash = 0.09
        targets = []
        for enemy in self.enemies:
            dx, dy = enemy[0] - self.x, enemy[1] - self.y
            angle = (math.atan2(dy, dx) - self.angle + math.pi) % math.tau - math.pi
            if abs(angle) < math.atan2(0.28, math.hypot(dx, dy)) and self.visible(
                *enemy[:2]
            ):
                targets.append(enemy)
        if targets:
            target = min(
                targets, key=lambda e: math.hypot(e[0] - self.x, e[1] - self.y)
            )
            target[2] -= 1
            self.status = "Hit!" if target[2] else "Sentry destroyed."
            self.enemies = [e for e in self.enemies if e[2] > 0]
        else:
            self.status = "Shot missed or blocked by a wall."
        if not self.enemies:
            self.finished = True
            self.status = "YOU WIN - arena cleared!"

    def handle(self, event):
        if event.type == pygame.KEYUP:
            self.keys.discard(event.key)
        elif event.type == pygame.KEYDOWN and not self.finished:
            self.keys.add(event.key)
            if event.key == pygame.K_SPACE:
                self.shoot()
        elif event.type == pygame.WINDOWFOCUSLOST:
            self.keys.clear()

    def update(self, dt):
        if self.finished:
            return
        dt = min(max(dt, 0), 0.05)
        self.cooldown = max(0, self.cooldown - dt)
        self.flash = max(0, self.flash - dt)
        self.angle += (
            (int(pygame.K_RIGHT in self.keys) - int(pygame.K_LEFT in self.keys))
            * dt
            * 1.8
        )
        forward = int(pygame.K_w in self.keys) - int(pygame.K_s in self.keys)
        strafe = int(pygame.K_d in self.keys) - int(pygame.K_a in self.keys)
        normal = max(1, math.hypot(forward, strafe))
        dx = (
            (math.cos(self.angle) * forward - math.sin(self.angle) * strafe)
            * dt
            * 2.8
            / normal
        )
        dy = (
            (math.sin(self.angle) * forward + math.cos(self.angle) * strafe)
            * dt
            * 2.8
            / normal
        )
        if self.can_stand(self.x + dx, self.y):
            self.x += dx
        if self.can_stand(self.x, self.y + dy):
            self.y += dy
        if pygame.K_SPACE in self.keys:
            self.shoot()
        for enemy in self.enemies:
            distance = math.hypot(self.x - enemy[0], self.y - enemy[1])
            if distance > 0.8 and self.visible(*enemy[:2]):
                ex = enemy[0] + (self.x - enemy[0]) / distance * dt * 0.6
                ey = enemy[1] + (self.y - enemy[1]) / distance * dt * 0.6
                if self.can_stand(ex, enemy[1]):
                    enemy[0] = ex
                if self.can_stand(enemy[0], ey):
                    enemy[1] = ey
        self.attack_clock += dt
        if self.attack_clock >= 0.8:
            self.attack_clock = 0
            damage = sum(
                8
                for e in self.enemies
                if math.hypot(e[0] - self.x, e[1] - self.y) < 4 and self.visible(*e[:2])
            )
            self.hp = max(0, self.hp - damage)
            if self.hp == 0:
                self.finished = True
                self.status = "GAME OVER - sentries defeated you."

    def draw(self, surface):
        heading(
            surface,
            "FPS / Sentry Arena",
            "WASD: move / strafe    Left/Right: turn    Space: shoot (hold allowed)",
        )
        pygame.draw.rect(surface, (40, 57, 83), (0, 105, 1000, 270))
        pygame.draw.rect(surface, (44, 43, 45), (0, 375, 1000, 255))
        depth = []
        for column in range(250):
            offset = math.atan((column / 249 - 0.5) * 2 * math.tan(math.pi / 6))
            distance = self.ray(self.angle + offset) * math.cos(offset)
            depth.append(distance)
            height = min(525, int(470 / max(0.1, distance)))
            shade = max(35, int(190 / (1 + distance * 0.1)))
            pygame.draw.rect(
                surface,
                (shade // 2, shade, shade),
                (column * 4, 375 - height // 2, 4, height),
            )
        for ex, ey, hp in sorted(
            self.enemies, key=lambda e: -math.hypot(e[0] - self.x, e[1] - self.y)
        ):
            dx, dy = ex - self.x, ey - self.y
            angle = (math.atan2(dy, dx) - self.angle + math.pi) % math.tau - math.pi
            if abs(angle) >= math.pi / 2:
                continue
            distance = math.hypot(dx, dy) * math.cos(angle)
            center = 500 + math.tan(angle) * 866
            height = min(510, int(400 / max(0.15, distance)))
            halfwidth = height * 0.28
            for column in range(
                max(0, int((center - halfwidth) / 4)),
                min(250, int((center + halfwidth) / 4) + 1),
            ):
                if depth[column] > distance:
                    pygame.draw.rect(
                        surface,
                        (220, 62 + hp * 20, 80),
                        (column * 4, 375 - height // 2, 4, height),
                    )
        pygame.draw.line(surface, (255, 245, 210), (487, 375), (513, 375), 2)
        pygame.draw.line(surface, (255, 245, 210), (500, 362), (500, 388), 2)
        pygame.draw.polygon(
            surface, (125, 140, 155), [(450, 630), (480, 540), (520, 540), (550, 630)]
        )
        if self.flash:
            pygame.draw.circle(surface, (255, 223, 85), (500, 530), 20)
        text(
            surface,
            f"HP {self.hp}    Sentries {len(self.enemies)}/4    3 hits per sentry",
            (30, 642),
        )
        text(surface, self.status, (30, 680), 20)
