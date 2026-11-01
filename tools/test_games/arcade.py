"""Asset-free keyboard arcade games for the visual game-player test launcher.

Each game owns its simulation and draws on a 1000 x 760 pygame surface. The
launcher reserves the bottom 40 pixels for menu/restart hints. Simulation time
advances only through update(dt); seeded randomness makes retries repeatable.
"""

import math
import random

import pygame


BG = (15, 22, 38)
INK = (231, 240, 250)
MUTED = (155, 176, 201)
ACCENT = (76, 219, 194)
COLORS = (
    (75, 211, 244),
    (245, 214, 83),
    (177, 117, 244),
    (83, 214, 139),
    (243, 99, 121),
    (87, 132, 245),
    (243, 164, 78),
)


def text(surface, label, pos, size=25, color=INK):
    font = pygame.font.Font(None, size)
    surface.blit(font.render(str(label), True, color), pos)


def header(surface, title, subtitle):
    surface.fill(BG)
    text(surface, title, (40, 23), 42)
    text(surface, subtitle, (40, 68), 24, MUTED)


def outcome(surface, status):
    if status != "Playing":
        panel = pygame.Surface((660, 115), pygame.SRCALPHA)
        panel.fill((8, 14, 27, 235))
        surface.blit(panel, (170, 280))
        text(surface, status, (210, 304), 40, ACCENT)
        text(surface, "R: restart   Esc: game menu", (210, 351), 25)


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
