# SPDX-License-Identifier: GPL-3.0-or-later
"""Shogi standalone game; rules preserved from the original suite."""

import random
import shogi
import pygame
from ..drawing_boards import has_japanese_font, header, text


class Shogi:
    """Nine-by-nine shogi with legal promotion choices and captured-piece drops."""

    labels = {
        1: "歩",
        2: "香",
        3: "桂",
        4: "銀",
        5: "金",
        6: "角",
        7: "飛",
        8: "王",
        9: "と",
        10: "杏",
        11: "圭",
        12: "全",
        13: "馬",
        14: "龍",
    }
    ascii_labels = {
        1: "P",
        2: "L",
        3: "N",
        4: "S",
        5: "G",
        6: "B",
        7: "R",
        8: "K",
        9: "+P",
        10: "+L",
        11: "+N",
        12: "+S",
        13: "+B",
        14: "+R",
    }

    def piece_label(self, piece_type):
        return (self.labels if has_japanese_font() else self.ascii_labels)[piece_type]

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.board = shogi.Board()
        self.selected = None
        self.drop = None
        self.promotion_choices = []
        self.wait = 0.35
        self.over = False
        self.status = "Your turn: Sente (bottom)"
        self.position_history = [self.position_key()]
        self.check_history = []

    def position_key(self):
        return " ".join(self.board.sfen().split()[:3])

    def refresh_status(self):
        if self.board.is_fourfold_repetition():
            self.over = True
            self.status = "Draw: fourfold repetition"
            repeats = [
                i
                for i, key in enumerate(self.position_history)
                if key == self.position_key()
            ]
            if len(repeats) >= 4:
                cycle = self.check_history[repeats[-4] :]
                for side in (shogi.BLACK, shogi.WHITE):
                    checks = [checked for mover, checked in cycle if mover == side]
                    if checks and all(checks):
                        self.status = (
                            "CPU wins" if side == shogi.BLACK else "You win"
                        ) + ": perpetual check"
                        break
        elif not list(self.board.legal_moves):
            self.over = True
            self.status = (
                "CPU wins: no legal move"
                if self.board.turn == shogi.BLACK
                else "You win: no legal move"
            )
        else:
            self.status = (
                "Your turn: Sente"
                if self.board.turn == shogi.BLACK
                else "CPU turn: Gote"
            )
            if self.board.is_check():
                self.status += " - CHECK"

    def commit(self, move):
        # Reset auxiliary history if a test replaces the board with an SFEN.
        if self.position_history[-1] != self.position_key():
            self.position_history = [self.position_key()]
            self.check_history = []
        mover = self.board.turn
        self.board.push(move)
        self.position_history.append(self.position_key())
        self.check_history.append((mover, self.board.is_check()))
        self.selected = self.drop = None
        self.promotion_choices = []
        self.wait = 0.35
        self.refresh_status()

    def hand_rect(self, piece_type):
        return pygame.Rect(
            610 + ((piece_type - 1) % 4) * 86,
            390 + ((piece_type - 1) // 4) * 65,
            80,
            58,
        )

    def handle(self, event):
        if (
            event.type != pygame.MOUSEBUTTONDOWN
            or event.button != 1
            or self.over
            or self.board.turn != shogi.BLACK
        ):
            return
        if self.promotion_choices:
            for promote, rect in (
                (True, pygame.Rect(610, 535, 160, 48)),
                (False, pygame.Rect(780, 535, 160, 48)),
            ):
                if rect.collidepoint(event.pos):
                    move = next(
                        m for m in self.promotion_choices if m.promotion == promote
                    )
                    self.commit(move)
            return
        for piece_type in range(1, 8):
            if self.hand_rect(piece_type).collidepoint(
                event.pos
            ) and self.board.pieces_in_hand[shogi.BLACK].get(piece_type, 0):
                self.drop, self.selected = piece_type, None
                return
        col, row = (event.pos[0] - 40) // 60, (event.pos[1] - 115) // 60
        if not (0 <= col < 9 and 0 <= row < 9):
            self.selected = self.drop = None
            return
        square = row * 9 + col
        legal = list(self.board.legal_moves)
        if self.drop is not None:
            choices = [
                m
                for m in legal
                if m.drop_piece_type == self.drop and m.to_square == square
            ]
        else:
            choices = [
                m
                for m in legal
                if m.from_square == self.selected
                and m.to_square == square
                and m.drop_piece_type is None
            ]
        if len(choices) == 1:
            self.commit(choices[0])
        elif len(choices) > 1:
            self.promotion_choices = choices
            self.status = "Choose promotion: 成る / 成らず"
        else:
            piece = self.board.piece_at(square)
            self.selected = square if piece and piece.color == shogi.BLACK else None
            self.drop = None

    def update(self, dt):
        if self.over or self.board.turn == shogi.BLACK:
            return
        self.wait -= dt
        if self.wait > 0:
            return
        moves = list(self.board.legal_moves)
        self.rng.shuffle(moves)
        values = {
            1: 100,
            2: 300,
            3: 320,
            4: 450,
            5: 550,
            6: 800,
            7: 1000,
            8: 20000,
            9: 550,
            10: 550,
            11: 550,
            12: 550,
            13: 1000,
            14: 1200,
        }

        def score(move):
            captured = self.board.piece_at(move.to_square)
            value = values[captured.piece_type] if captured else 0
            if move.promotion:
                value += 150
            # A one-ply material opponent keeps input responsive even with drops.
            return value

        self.commit(max(moves, key=score))

    def draw(self, surface):
        header(surface, "Shogi / 将棋", self.status)
        legal = list(self.board.legal_moves) if not self.over else []
        targets = {
            m.to_square
            for m in legal
            if (self.drop is not None and m.drop_piece_type == self.drop)
            or (
                self.drop is None
                and self.selected is not None
                and m.from_square == self.selected
            )
        }
        for row in range(9):
            for col in range(9):
                square = row * 9 + col
                rect = pygame.Rect(40 + col * 60, 115 + row * 60, 60, 60)
                pygame.draw.rect(surface, (205, 166, 106), rect)
                pygame.draw.rect(surface, (93, 65, 32), rect, 1)
                if square == self.selected:
                    pygame.draw.rect(surface, (80, 160, 210), rect, 4)
                piece = self.board.piece_at(square)
                if piece:
                    tile = pygame.Surface((52, 52), pygame.SRCALPHA)
                    pygame.draw.polygon(
                        tile,
                        (241, 210, 151),
                        ((3, 49), (5, 10), (26, 1), (47, 10), (49, 49)),
                    )
                    text(
                        tile,
                        self.piece_label(piece.piece_type),
                        9,
                        6,
                        30,
                        (146, 36, 31) if piece.piece_type >= 9 else (33, 30, 25),
                    )
                    if piece.color == shogi.WHITE:
                        tile = pygame.transform.rotate(tile, 180)
                    surface.blit(tile, (rect.x + 4, rect.y + 4))
                if square in targets:
                    pygame.draw.circle(
                        surface, (38, 133, 117), (rect.right - 9, rect.bottom - 9), 5
                    )
        for i in range(9):
            text(surface, 9 - i, 63 + i * 60, 660, 17)
        for i, line in enumerate(
            (
                "You: bottom / CPU: top",
                "Click piece, then destination.",
                "Click your hand to select a drop.",
                "Optional promotion: choose below.",
                "Green dots: legal destinations.",
                "Your captured pieces / 持ち駒:",
            )
        ):
            text(surface, line, 610, 130 + i * 43, 18)
        for piece_type in range(1, 8):
            rect = self.hand_rect(piece_type)
            pygame.draw.rect(
                surface,
                (65, 105, 121) if self.drop == piece_type else (49, 59, 76),
                rect,
                border_radius=5,
            )
            count = self.board.pieces_in_hand[shogi.BLACK].get(piece_type, 0)
            text(
                surface,
                f"{self.piece_label(piece_type)} {count}",
                rect.x + 10,
                rect.y + 10,
                23,
            )
        if self.promotion_choices:
            for label, rect in (
                ("成る / Yes", pygame.Rect(610, 535, 160, 48)),
                ("成らず / No", pygame.Rect(780, 535, 160, 48)),
            ):
                pygame.draw.rect(surface, (65, 105, 121), rect, border_radius=5)
                text(surface, label, rect.x + 9, rect.y + 9, 21)
        cpu_hand = " ".join(
            f"{self.piece_label(p)}{n}"
            for p, n in sorted(self.board.pieces_in_hand[shogi.WHITE].items())
            if n
        )
        text(surface, "CPU hand: " + (cpu_hand or "none"), 610, 610, 18)
