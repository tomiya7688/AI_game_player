# SPDX-License-Identifier: GPL-3.0-or-later
"""Mouse-operated board games with local, bounded CPU opponents.

The player always moves first. Chess and shogi delegate legal-move generation
to their respective OSS libraries; the CPU chooses among those legal moves.
"""

from functools import lru_cache

import pygame

JAPANESE_FONT_FAMILIES = "meiryo,yugothic,notosanscjkjp,notoserifcjkjp"


@lru_cache(maxsize=32)
def font(size):
    return pygame.font.SysFont(JAPANESE_FONT_FAMILIES + ",dejavusans,arial", size)


@lru_cache(maxsize=1)
def has_japanese_font():
    return pygame.font.match_font(JAPANESE_FONT_FAMILIES) is not None


def text(surface, message, x, y, size=22, color=(235, 239, 247)):
    surface.blit(font(size).render(str(message), True, color), (x, y))


def header(surface, title, status):
    surface.fill((20, 27, 42))
    text(surface, title, 30, 15, 30)
    text(surface, status, 30, 59, 22, (116, 223, 188))
