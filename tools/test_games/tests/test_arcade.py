# SPDX-License-Identifier: GPL-3.0-or-later
"""Rule and terminal-state checks for the standalone arcade test games."""

import pygame

from tools.test_games.arcade import Breakout, Shooter, Tetris


def test_tetris_seven_bag_and_seed_reproducibility():
    a, b = Tetris(42), Tetris(42)
    sequence_a = [a.kind, a.next_kind] + [a._take() for _ in range(12)]
    sequence_b = [b.kind, b.next_kind] + [b._take() for _ in range(12)]
    assert sequence_a == sequence_b
    assert set(sequence_a[:7]) == set(range(7))
    assert set(sequence_a[7:14]) == set(range(7))


def test_tetris_hard_drop_clears_fortieth_line_and_stops():
    game = Tetris()
    game.lines = 39
    game.board[-1] = [1] * 6 + [0] * 4
    game.kind = 0
    game.cells = list(game.SHAPES[0])
    game.x, game.y = 6, 0
    game.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_SPACE))
    assert game.lines == 40
    assert game.status == "40 lines cleared!"
    before = [row[:] for row in game.board]
    game.update(5)
    game.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_LEFT))
    assert game.board == before
    assert game.elapsed == 0


def test_tetris_collision_and_spawn_loss():
    game = Tetris()
    assert not game._fits(game.cells, -8, 0)
    assert not game._fits(game.cells, 0, 20)
    game.board[:4] = [[1] * 10 for _ in range(4)]
    game._spawn()
    assert game.status == "Game over"


def test_tetris_rotation_keeps_four_distinct_cells_inside_board():
    game = Tetris()
    for kind in range(7):
        game.kind = kind
        game.cells = list(game.SHAPES[kind])
        game.x, game.y = 0, 16
        for _ in range(4):
            game._rotate()
            assert len(set(game.cells)) == 4
            assert game._fits(game.cells, game.x, game.y)


def test_shooter_projectile_collision_and_last_wave_win():
    game = Shooter()
    game.wave = 3
    game.enemies = [[pygame.Vector2(500, 200), 0]]
    game.shots = [pygame.Vector2(500, 210)]
    game.update(0.02)
    assert game.kills == 1
    assert game.status == "All 3 waves cleared!"
    elapsed = game.elapsed
    game.update(1)
    assert game.elapsed == elapsed


def test_shooter_loss_and_invulnerability_prevent_multiple_hits():
    game = Shooter()
    game.enemy_shots = [game.player.copy(), game.player.copy()]
    game.update(0.01)
    assert game.lives == 2
    game.lives = 1
    game.invincible = 0
    game.enemy_shots = [game.player.copy()]
    game.update(0.01)
    assert game.status == "Game over"


def test_breakout_last_brick_collision_wins():
    game = Breakout()
    game.bricks = [pygame.Rect(400, 200, 80, 24)]
    game.ball.update(440, 235)
    game.velocity.update(0, -380)
    game.launched = True
    game.update(0.03)
    assert game.score == 1
    assert game.status == "All bricks cleared!"


def test_breakout_paddle_bounces_up_and_last_miss_loses():
    game = Breakout()
    game.ball.update(500, 635)
    game.velocity.update(0, 380)
    game.launched = True
    game.update(0.02)
    assert game.velocity.y < 0
    game.lives = 1
    game.ball.update(100, 709)
    game.velocity.update(0, 380)
    game.update(0.02)
    assert game.status == "Game over"


def test_focus_loss_releases_all_held_controls():
    for cls in (Tetris, Shooter, Breakout):
        game = cls()
        game.handle(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_LEFT))
        game.handle(pygame.event.Event(pygame.WINDOWFOCUSLOST))
        assert not game.held
