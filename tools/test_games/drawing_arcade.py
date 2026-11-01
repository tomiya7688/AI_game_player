# SPDX-License-Identifier: GPL-3.0-or-later
"""Asset-free keyboard arcade games for the visual game-player test launcher.

Each game owns its simulation and draws on a 1000 x 760 pygame surface. The
launcher reserves the bottom 40 pixels for menu/restart hints. Simulation time
advances only through update(dt); seeded randomness makes retries repeatable.
"""

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
