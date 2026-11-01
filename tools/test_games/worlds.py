"""Asset-free dungeon, first-person arena, and block playground for GUI testing.

Every game accepts pygame events and draws within 1000 x 720 pixels. The suite
owns the final forty pixels, Escape, and R. No game reads the AI player's state.
"""

import math
import random

import pygame


def text(surface, message, pos, size=24, color=(223, 233, 245)):
    surface.blit(
        pygame.font.SysFont("consolas", size).render(message, True, color), pos
    )


def heading(surface, title, controls):
    surface.fill((15, 23, 38))
    text(surface, title, (30, 18), 32, (95, 218, 204))
    text(surface, controls, (30, 62), 18)


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
