# SPDX-License-Identifier: GPL-3.0-or-later
"""Roguelike standalone game; rules preserved from the original suite."""

import random
import pygame
from ..drawing_worlds import heading, text


class Roguelike:
    """Turn-based connected maze; bump monsters to attack, reach stairs to win."""

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.width, self.height = 25, 17
        self.floor = {(1, 1)}
        stack = [(1, 1)]
        while stack:
            x, y = stack[-1]
            neighbors = [
                (x + dx, y + dy)
                for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2))
                if 0 < x + dx < self.width - 1
                and 0 < y + dy < self.height - 1
                and (x + dx, y + dy) not in self.floor
            ]
            if not neighbors:
                stack.pop()
                continue
            nx, ny = self.rng.choice(neighbors)
            self.floor.update({(nx, ny), ((x + nx) // 2, (y + ny) // 2)})
            stack.append((nx, ny))
        self.player = (1, 1)
        # Distances select stairs at the far end of the connected dungeon.
        distance = {self.player: 0}
        queue = [self.player]
        for cell in queue:
            for candidate in self.neighbors(cell):
                if candidate not in distance:
                    distance[candidate] = distance[cell] + 1
                    queue.append(candidate)
        self.stairs = max(distance, key=distance.get)
        available = sorted(self.floor - {self.player, self.stairs})
        self.monsters = {cell: 2 for cell in self.rng.sample(available, 8)}
        self.potions = set(
            self.rng.sample(sorted(set(available) - self.monsters.keys()), 7)
        )
        self.hp = 24
        self.turns = 0
        self.kills = 0
        self.status = "Find the green stairs. Bump red monsters to attack."
        self.finished = False

    def neighbors(self, cell):
        x, y = cell
        return [
            pos
            for pos in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
            if pos in self.floor
        ]

    def handle(self, event):
        if self.finished or event.type != pygame.KEYDOWN:
            return
        moves = {
            pygame.K_LEFT: (-1, 0),
            pygame.K_a: (-1, 0),
            pygame.K_RIGHT: (1, 0),
            pygame.K_d: (1, 0),
            pygame.K_UP: (0, -1),
            pygame.K_w: (0, -1),
            pygame.K_DOWN: (0, 1),
            pygame.K_s: (0, 1),
            pygame.K_SPACE: (0, 0),
        }
        if event.key not in moves:
            return
        dx, dy = moves[event.key]
        target = (self.player[0] + dx, self.player[1] + dy)
        if target not in self.floor:
            self.status = "A stone wall blocks that direction."
            return
        if target in self.monsters:
            self.monsters[target] -= 1
            self.status = "You strike a monster."
            if self.monsters[target] <= 0:
                del self.monsters[target]
                self.kills += 1
                self.status = "Monster defeated."
        else:
            self.player = target
            if target in self.potions:
                self.potions.remove(target)
                self.hp = min(24, self.hp + 8)
                self.status = "Healing potion: +8 HP (maximum 24)."
        self.turns += 1
        if self.player == self.stairs:
            self.status = "YOU WIN - escaped the dungeon!"
            self.finished = True
            return
        occupied = set(self.monsters)
        for position, hp in list(self.monsters.items()):
            if (
                abs(position[0] - self.player[0]) + abs(position[1] - self.player[1])
                == 1
            ):
                self.hp -= 1
            elif self.turns % 2 == 0:
                options = [
                    p
                    for p in self.neighbors(position)
                    if p not in occupied and p != self.player
                ]
                if options:
                    nxt = min(
                        options,
                        key=lambda p: (
                            abs(p[0] - self.player[0]) + abs(p[1] - self.player[1])
                        ),
                    )
                    if abs(nxt[0] - self.player[0]) + abs(nxt[1] - self.player[1]) < 7:
                        occupied.remove(position)
                        occupied.add(nxt)
                        del self.monsters[position]
                        self.monsters[nxt] = hp
        if self.hp <= 0:
            self.status = "GAME OVER - your explorer fell. R starts again."
            self.finished = True

    def update(self, dt):
        pass  # Dungeon time advances only when the player takes a valid turn.

    def draw(self, surface):
        heading(
            surface,
            "ROGUELIKE / The Emerald Stair",
            "Arrows or WASD: move / bump to attack    Space: wait",
        )
        for y in range(self.height):
            for x in range(self.width):
                color = (44, 55, 71) if (x, y) not in self.floor else (23, 32, 45)
                pygame.draw.rect(surface, color, (50 + x * 36, 105 + y * 30, 35, 29))
        for (x, y), glyph, color in [
            (self.stairs, ">", (90, 240, 150)),
            *[(p, "+", (240, 190, 80)) for p in self.potions],
            *[(p, "M", (245, 100, 115)) for p in self.monsters],
            (self.player, "@", (130, 210, 255)),
        ]:
            text(surface, glyph, (59 + x * 36, 106 + y * 30), 25, color)
        text(
            surface,
            f"HP {self.hp}/24   Turns {self.turns}   Defeated {self.kills}/8",
            (50, 628),
        )
        text(surface, self.status, (50, 667), 20)
