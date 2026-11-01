# SPDX-License-Identifier: GPL-3.0-or-later
"""World-game rule checks; no display or real input is required."""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame

from tools.test_games.worlds import FPS, Roguelike, Sandbox


def key(code):
    return pygame.event.Event(pygame.KEYDOWN, key=code)


def test_dungeon_stairs_and_all_tiles_are_reachable():
    game = Roguelike(17)
    visited = {game.player}
    queue = [game.player]
    for cell in queue:
        for nxt in game.neighbors(cell):
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)
    assert visited == game.floor
    assert game.stairs in visited
    assert game.player != game.stairs


def test_dungeon_wall_does_not_advance_turn_and_stairs_win():
    game = Roguelike()
    game.handle(key(pygame.K_LEFT))
    assert game.turns == 0
    assert game.player == (1, 1)
    game.monsters.clear()
    target = game.neighbors(game.player)[0]
    game.stairs = target
    delta = (target[0] - 1, target[1] - 1)
    code = {(1, 0): pygame.K_RIGHT, (0, 1): pygame.K_DOWN}[delta]
    game.handle(key(code))
    assert game.finished
    assert "WIN" in game.status
    game.handle(key(pygame.K_SPACE))
    assert game.turns == 1


def test_dungeon_monster_requires_two_attacks_and_cannot_be_walked_through():
    game = Roguelike()
    target = game.neighbors(game.player)[0]
    game.monsters = {target: 2}
    code = pygame.K_RIGHT if target[0] == 2 else pygame.K_DOWN
    game.handle(key(code))
    assert game.monsters[target] == 1
    assert game.player == (1, 1)
    game.handle(key(code))
    assert target not in game.monsters
    assert game.kills == 1


def test_fps_wall_blocks_hits_and_nearest_enemy_takes_damage():
    game = FPS()
    game.x, game.y, game.angle = 1.5, 2.5, 0
    game.enemies = [[6.5, 2.5, 3]]  # Two solid blocks stand between player and enemy.
    game.shoot()
    assert game.enemies[0][2] == 3
    game.x, game.y = 2.5, 1.5
    game.enemies = [[4.5, 1.5, 3], [6.5, 1.5, 3]]
    game.cooldown = 0
    game.shoot()
    assert [enemy[2] for enemy in game.enemies] == [2, 3]


def test_fps_win_freezes_movement_and_wall_collision():
    game = FPS()
    assert not game.can_stand(0.1, 1.5)
    game.enemies = [[3.5, 1.5, 1]]
    game.shoot()
    assert game.finished
    assert "WIN" in game.status
    game.keys.add(pygame.K_w)
    game.update(1)
    assert (game.x, game.y) == (2.5, 1.5)


def test_fps_damage_can_end_game():
    game = FPS()
    game.enemies = [[3.5, 1.5, 3]]
    game.hp = 8
    game.attack_clock = 0.79
    game.update(0.02)
    assert game.hp == 0
    assert game.finished


def test_sandbox_mining_and_placing_preserve_inventory_and_player_space():
    game = Sandbox()
    game.x, game.y = 3.5, 6.5
    game.blocks[(4, 6)] = 2
    pixel = (52 + 4 * 28 + 5, 105 + 6 * 28 + 5)
    game.edit(pixel, 1)
    assert (4, 6) not in game.blocks
    assert game.inventory[2] == 11
    game.selected = 2
    game.edit(pixel, 3)
    assert game.blocks[(4, 6)] == 2
    assert game.inventory[2] == 10
    game.edit((52 + 3 * 28 + 5, 105 + 6 * 28 + 5), 3)
    assert (3, 6) not in game.blocks
    assert game.inventory[2] == 10


def test_sandbox_reach_and_bedrock_are_enforced():
    game = Sandbox()
    before = dict(game.blocks)
    game.edit((52 + 25 * 28, 105 + 12 * 28), 1)
    assert game.blocks == before
    game.x, game.y = 3.5, 17.5
    game.edit((52 + 3 * 28 + 5, 105 + 18 * 28 + 5), 1)
    assert game.blocks == before


def test_sandbox_gravity_lands_without_entering_terrain():
    game = Sandbox()
    for _ in range(200):
        game.update(0.03)
    assert game.can_stand(game.x, game.y)
    assert not game.can_stand(game.x, game.y + 0.1)


def test_worlds_are_seeded_and_can_render():
    pygame.font.init()
    surface = pygame.Surface((1000, 760))
    for cls in (Roguelike, FPS, Sandbox):
        game = cls(seed=31)
        game.draw(surface)
        assert game.status
    assert Roguelike(31).floor == Roguelike(31).floor
    assert Sandbox(31).blocks == Sandbox(31).blocks
