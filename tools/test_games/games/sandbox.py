# SPDX-License-Identifier: GPL-3.0-or-later
"""Sandbox standalone game; rules preserved from the original suite."""

import math
import random
import pygame
from ..drawing_worlds import heading, text


class Sandbox:
    """Finite side-view world with solid terrain, jumping, mining and placement."""

    COLORS = {1: (140, 94, 59), 2: (110, 126, 143), 3: (182, 138, 71)}

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.width, self.height, self.tile = 32, 19, 28
        self.blocks = {}
        for x in range(self.width):
            ground = 11 + self.rng.choice((-1, 0, 0, 1))
            for y in range(ground, self.height):
                self.blocks[x, y] = 1 if y < ground + 3 else 2
        self.x, self.y = 3.5, 6.5
        self.vy = 0
        self.keys = set()
        self.inventory = {1: 10, 2: 10, 3: 10}
        self.selected = 1
        self.status = "Open-ended world: mine blocks and build your own shelter."

    def solid(self, x, y):
        return (
            x < 0
            or x >= self.width
            or y >= self.height
            or (math.floor(x), math.floor(y)) in self.blocks
        )

    def can_stand(self, x, y):
        return all(
            not self.solid(x + dx, y + dy) for dx in (-0.28, 0.28) for dy in (-0.4, 0.4)
        )

    def edit(self, pos, button):
        x, y = (pos[0] - 52) // self.tile, (pos[1] - 105) // self.tile
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        if math.hypot(x + 0.5 - self.x, y + 0.5 - self.y) > 4:
            self.status = "Out of reach: move within four blocks."
            return
        if y == self.height - 1:
            self.status = "The bottom bedrock cannot be changed."
            return
        if button == 1 and (x, y) in self.blocks:
            material = self.blocks.pop((x, y))
            self.inventory[material] += 1
            self.status = "Block mined and added to your inventory."
        elif button == 3 and (x, y) not in self.blocks:
            if self.inventory[self.selected] <= 0:
                self.status = "No blocks of that material remain. Mine more first."
                return
            self.blocks[x, y] = self.selected
            if not self.can_stand(self.x, self.y):
                del self.blocks[x, y]
                self.status = "Cannot place a block inside the player."
            else:
                self.inventory[self.selected] -= 1
                self.status = "Block placed."

    def handle(self, event):
        if event.type == pygame.KEYUP:
            self.keys.discard(event.key)
        elif event.type == pygame.KEYDOWN:
            self.keys.add(event.key)
            if event.key in (pygame.K_1, pygame.K_2, pygame.K_3):
                self.selected = event.key - pygame.K_1 + 1
            if event.key in (
                pygame.K_SPACE,
                pygame.K_w,
                pygame.K_UP,
            ) and not self.can_stand(self.x, self.y + 0.07):
                self.vy = -7.5
        elif event.type == pygame.MOUSEBUTTONDOWN:
            self.edit(event.pos, event.button)
        elif event.type == pygame.WINDOWFOCUSLOST:
            self.keys.clear()

    def update(self, dt):
        dt = min(max(dt, 0), 0.05)
        # Four small collision steps prevent tunnelling through single blocks.
        for _ in range(4):
            step = dt / 4
            direction = int(
                pygame.K_d in self.keys or pygame.K_RIGHT in self.keys
            ) - int(pygame.K_a in self.keys or pygame.K_LEFT in self.keys)
            nx = self.x + direction * step * 5
            if self.can_stand(nx, self.y):
                self.x = nx
            self.vy = min(12, self.vy + 18 * step)
            ny = max(0.45, self.y + self.vy * step)
            if self.can_stand(self.x, ny):
                self.y = ny
            else:
                self.vy = 0

    def draw(self, surface):
        heading(
            surface,
            "SANDBOX / Pocket Blocks",
            "A/D or arrows: move    Space: jump    Left click: mine    Right click: place",
        )
        pygame.draw.rect(
            surface,
            (70, 125, 160),
            (52, 105, self.width * self.tile, self.height * self.tile),
        )
        pygame.draw.circle(surface, (250, 223, 130), (840, 165), 32)
        for (x, y), material in self.blocks.items():
            color = (37, 45, 56) if y == self.height - 1 else self.COLORS[material]
            rect = pygame.Rect(
                52 + x * self.tile, 105 + y * self.tile, self.tile - 1, self.tile - 1
            )
            pygame.draw.rect(surface, color, rect)
            if material == 1 and (x, y - 1) not in self.blocks:
                pygame.draw.rect(
                    surface, (90, 165, 80), (rect.x, rect.y, rect.width, 5)
                )
        pygame.draw.rect(
            surface,
            (255, 222, 120),
            (
                52 + (self.x - 0.28) * self.tile,
                105 + (self.y - 0.4) * self.tile,
                16,
                23,
            ),
            border_radius=4,
        )
        names = {1: "Dirt", 2: "Stone", 3: "Wood"}
        text(
            surface,
            "1/2/3 select  "
            + "   ".join(
                f"{'>' if self.selected == m else ' '} {m}:{names[m]} {self.inventory[m]}"
                for m in names
            ),
            (35, 647),
            21,
        )
        text(surface, self.status, (35, 685), 19)
