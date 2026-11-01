# SPDX-License-Identifier: GPL-3.0-or-later
"""Rule boundaries and GUI-to-legal-move integration for the test games."""

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import chess
import pygame
import shogi

from tools.test_games.boards import Chess, Gomoku, Othello, Shogi
from tools.test_games.games import shogi as shogi_game


def click(game, x, y):
    game.handle(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(x, y)))


def test_othello_initial_moves_and_capture():
    game = Othello()
    assert set(game.legal(1)) == {(3, 2), (2, 3), (5, 4), (4, 5)}
    assert not game.play(0, 0)
    assert game.play(3, 2)
    assert game.board[3][3] == 1
    assert game.turn == -1
    game.update(1)
    assert game.turn == 1


def test_othello_both_sides_without_moves_finishes():
    game = Othello()
    game.board = [[1] * 8 for _ in range(8)]
    game.board[0][0], game.board[0][1] = 0, -1
    game.play(0, 0)
    assert game.over
    assert "You win" in game.status


def test_gomoku_cpu_blocks_immediate_win():
    game = Gomoku()
    for x in range(4):
        game.board[7][x] = 1
    game.turn = -1
    game.update(1)
    assert game.board[7][4] == -1


def test_gomoku_five_and_overline_are_wins():
    game = Gomoku()
    for x in range(6):
        game.board[0][x] = 1
    assert game.won(5, 0, 1)
    assert not game.play(-1, 0)


def test_chess_click_rejects_illegal_destination_and_cpu_replies():
    game = Chess()
    click(game, 40 + 4 * 64 + 32, 115 + 6 * 64 + 32)  # e2
    click(game, 40 + 4 * 64 + 32, 115 + 3 * 64 + 32)  # e5 illegal
    assert game.board.ply() == 0
    click(game, 40 + 4 * 64 + 32, 115 + 6 * 64 + 32)
    click(game, 40 + 4 * 64 + 32, 115 + 4 * 64 + 32)  # e4
    assert game.board.ply() == 1
    game.update(1)
    assert game.board.ply() == 2
    assert game.board.is_valid()


def test_chess_promotion_selects_queen_and_terminal_freezes():
    game = Chess()
    game.board = chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1")
    click(game, 72, 115 + 64 + 32)
    click(game, 72, 147)
    assert game.board.piece_at(chess.A8).piece_type == chess.QUEEN
    game.board = chess.Board("7k/6Q1/5K2/8/8/8/8/8 b - - 0 1")
    game.refresh_status()
    before = game.board.fen()
    game.update(1)
    assert game.board.fen() == before
    assert "You win" in game.status


def test_shogi_player_move_and_cpu_reply_are_legal():
    game = Shogi()
    click(game, 40 + 2 * 60 + 30, 115 + 6 * 60 + 30)  # 7g
    click(game, 40 + 2 * 60 + 30, 115 + 5 * 60 + 30)  # 7f
    assert game.board.move_number == 2
    assert game.board.turn == shogi.WHITE
    game.update(1)
    assert game.board.move_number == 3
    assert game.board.turn == shogi.BLACK


def test_shogi_piece_identity_remains_visible_without_japanese_fonts(monkeypatch):
    monkeypatch.setattr(shogi_game, "has_japanese_font", lambda: False)
    game = Shogi()
    assert game.piece_label(shogi.PAWN) == "P"
    assert game.piece_label(shogi.PROM_PAWN) == "+P"
    assert game.piece_label(shogi.KING) == "K"
    monkeypatch.setattr(shogi_game, "has_japanese_font", lambda: True)
    assert game.piece_label(shogi.PAWN) == "歩"


def test_shogi_nifu_and_mandatory_promotion():
    game = Shogi()
    game.board.set_sfen("4k4/9/9/9/9/9/PPPPPPPPP/9/4K4 b P 1")
    assert not any(m.drop_piece_type == shogi.PAWN for m in game.board.legal_moves)
    game.board.set_sfen("8k/P8/9/9/9/9/9/9/4K4 b - 1")
    legal = [
        m for m in game.board.legal_moves if m.from_square == 9 and m.to_square == 0
    ]
    assert len(legal) == 1 and legal[0].promotion
    click(game, 70, 205)
    click(game, 70, 145)
    assert game.board.piece_at(0).piece_type == shogi.PROM_PAWN


def test_shogi_optional_promotion_can_be_declined():
    game = Shogi()
    game.board.set_sfen("8k/9/P8/9/9/9/9/9/4K4 b - 1")
    click(game, 70, 265)
    click(game, 70, 205)
    assert len(game.promotion_choices) == 2
    click(game, 820, 550)
    assert game.board.piece_at(9).piece_type == shogi.PAWN
    assert game.board.turn == shogi.WHITE


def test_shogi_pawn_drop_mate_is_rejected():
    game = Shogi()
    game.board.set_sfen("4k4/9/4G4/9/9/9/9/9/3RKR3 b P 1")
    assert shogi.Move.from_usi("P*5b") not in game.board.legal_moves
    click(game, 650, 415)
    click(game, 40 + 4 * 60 + 30, 115 + 60 + 30)
    assert game.board.move_number == 1


def test_boards_draw_on_launcher_surface():
    pygame.init()
    surface = pygame.Surface((1000, 760))
    for cls in (Othello, Gomoku, Chess, Shogi):
        game = cls()
        game.draw(surface)
        assert game.status
    pygame.quit()


def test_shogi_repetition_by_continuous_check_is_loss():
    game = Shogi()
    game.board.set_sfen("4k4/5R3/9/9/9/9/9/9/4K4 b - 1")
    for _ in range(3):
        for usi in ("4b5b", "5a4a", "5b4b", "4a5a"):
            move = shogi.Move.from_usi(usi)
            assert move in game.board.legal_moves
            game.commit(move)
    assert game.over
    assert game.status == "CPU wins: perpetual check"


def test_shogi_repetition_without_check_is_draw():
    game = Shogi()
    game.board.set_sfen("4k4/9/9/9/9/9/9/9/4K4 b - 1")
    for _ in range(3):
        for usi in ("5i6i", "5a6a", "6i5i", "6a5a"):
            game.commit(shogi.Move.from_usi(usi))
    assert game.over
    assert game.status == "Draw: fourfold repetition"
