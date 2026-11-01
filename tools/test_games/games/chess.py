# SPDX-License-Identifier: GPL-3.0-or-later
"""Chess standalone game; rules preserved from the original suite."""

import random
import chess
import pygame
from ..drawing_boards import header, text


class Chess:
    """Standard chess legal moves; player chooses a piece then its destination."""

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.board = chess.Board()
        self.selected = None
        self.wait = 0.35
        self.status = "Your turn: White"

    def refresh_status(self):
        outcome = self.board.outcome(claim_draw=True)
        if outcome:
            result = (
                "Draw"
                if outcome.winner is None
                else "You win"
                if outcome.winner
                else "CPU wins"
            )
            self.status = f"{result}: {outcome.termination.name.lower()}"
        else:
            self.status = "Your turn: White" if self.board.turn else "CPU turn: Black"
            if self.board.is_check():
                self.status += " - CHECK"

    def handle(self, event):
        if (
            event.type != pygame.MOUSEBUTTONDOWN
            or event.button != 1
            or not self.board.turn
            or self.board.is_game_over(claim_draw=True)
        ):
            return
        x, y = (event.pos[0] - 40) // 64, (event.pos[1] - 115) // 64
        if not (0 <= x < 8 and 0 <= y < 8):
            return
        square = chess.square(x, 7 - y)
        if self.selected is not None:
            choices = [
                m
                for m in self.board.legal_moves
                if m.from_square == self.selected and m.to_square == square
            ]
            if choices:
                move = next(
                    (m for m in choices if m.promotion == chess.QUEEN), choices[0]
                )
                self.board.push(move)
                self.selected = None
                self.wait = 0.35
                self.refresh_status()
                return
        piece = self.board.piece_at(square)
        self.selected = square if piece and piece.color == chess.WHITE else None

    def update(self, dt):
        if self.board.turn or self.board.is_game_over(claim_draw=True):
            return
        self.wait -= dt
        if self.wait > 0:
            return
        moves = list(self.board.legal_moves)
        self.rng.shuffle(moves)
        values = {
            chess.PAWN: 100,
            chess.KNIGHT: 320,
            chess.BISHOP: 330,
            chess.ROOK: 500,
            chess.QUEEN: 900,
            chess.KING: 0,
        }

        def score(move):
            captured = self.board.piece_at(move.to_square)
            value = (
                values[captured.piece_type]
                if captured
                else 100
                if self.board.is_en_passant(move)
                else 0
            )
            value += values.get(move.promotion, 0)
            self.board.push(move)
            if self.board.is_checkmate():
                value += 100000
            moved = self.board.piece_at(move.to_square)
            if self.board.is_attacked_by(chess.WHITE, move.to_square):
                value -= values[moved.piece_type]
            if self.board.is_check():
                value += 30
            self.board.pop()
            return value

        self.board.push(max(moves, key=score))
        self.refresh_status()

    def draw(self, surface):
        header(surface, "Chess / チェス", self.status)
        targets = (
            {
                m.to_square
                for m in self.board.legal_moves
                if m.from_square == self.selected
            }
            if self.selected is not None
            else set()
        )
        for row in range(8):
            for col in range(8):
                square = chess.square(col, 7 - row)
                rect = pygame.Rect(40 + col * 64, 115 + row * 64, 64, 64)
                color = (217, 209, 180) if (row + col) % 2 == 0 else (96, 132, 115)
                pygame.draw.rect(surface, color, rect)
                if square == self.selected:
                    pygame.draw.rect(surface, (247, 194, 64), rect, 4)
                piece = self.board.piece_at(square)
                if piece:
                    # Letter labels avoid missing chess-symbol glyphs on Windows.
                    pygame.draw.circle(
                        surface,
                        (245, 244, 232) if piece.color else (29, 34, 42),
                        rect.center,
                        25,
                    )
                    text(
                        surface,
                        piece.symbol().upper(),
                        rect.x + 19,
                        rect.y + 10,
                        29,
                        (25, 30, 38) if piece.color else (246, 241, 222),
                    )
                elif square in targets:
                    pygame.draw.circle(surface, (39, 83, 67), rect.center, 7)
        for i in range(8):
            text(surface, "abcdefgh"[i], 64 + i * 64, 635, 18)
            text(surface, 8 - i, 16, 134 + i * 64, 18)
        for i, line in enumerate(
            (
                "You: White / CPU: Black",
                "Click piece, then destination.",
                "K king / Q queen / R rook",
                "B bishop / N knight / P pawn",
                "Promotion: queen automatically.",
                "Castling: select king destination.",
                "Claimable draws: automatic.",
            )
        ):
            text(surface, line, 590, 130 + i * 40, 19)
