# SPDX-License-Identifier: GPL-3.0-or-later
"""Asset-free dungeon, first-person arena, and block playground for GUI testing.

Every game accepts pygame events and draws within 1000 x 720 pixels. The suite
owns the final forty pixels, Escape, and R. No game reads the AI player's state.
"""

import pygame


def text(surface, message, pos, size=24, color=(223, 233, 245)):
    surface.blit(
        pygame.font.SysFont("consolas", size).render(message, True, color), pos
    )


def heading(surface, title, controls):
    surface.fill((15, 23, 38))
    text(surface, title, (30, 18), 32, (95, 218, 204))
    text(surface, controls, (30, 62), 18)
