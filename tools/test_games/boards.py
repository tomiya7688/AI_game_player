"""Mouse-operated board games with local, bounded CPU opponents.

The player always moves first. Chess and shogi delegate legal-move generation
to their respective OSS libraries; the CPU chooses among those legal moves.
"""

import random
from functools import lru_cache

import chess
import pygame
import shogi


@lru_cache(maxsize=32)
def font(size):
    return pygame.font.SysFont("meiryo,yugothic,dejavusans,arial", size)


def text(surface, message, x, y, size=22, color=(235, 239, 247)):
    surface.blit(font(size).render(str(message), True, color), (x, y))


def header(surface, title, status):
    surface.fill((20, 27, 42))
    text(surface, title, 30, 15, 30)
    text(surface, status, 30, 59, 22, (116, 223, 188))


class Othello:
    """Eight-by-eight Othello; automatic passes and final disk counts."""

    origin = (40, 115)
    cell = 64

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.board = [[0] * 8 for _ in range(8)]
        self.board[3][3] = self.board[4][4] = -1
        self.board[3][4] = self.board[4][3] = 1
        self.turn = 1
        self.over = False
        self.status = "Your turn: black"
        self.wait = 0.35

    def flips(self, x, y, side):
        if not (0 <= x < 8 and 0 <= y < 8) or self.board[y][x]:
            return []
        result = []
        for dx, dy in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),
        ):
            cx, cy = x + dx, y + dy
            line = []
            while 0 <= cx < 8 and 0 <= cy < 8 and self.board[cy][cx] == -side:
                line.append((cx, cy))
                cx, cy = cx + dx, cy + dy
            if line and 0 <= cx < 8 and 0 <= cy < 8 and self.board[cy][cx] == side:
                result.extend(line)
        return result

    def legal(self, side):
        return [(x, y) for y in range(8) for x in range(8) if self.flips(x, y, side)]

    def play(self, x, y):
        captured = self.flips(x, y, self.turn)
        if not captured or self.over:
            return False
        for cx, cy in captured + [(x, y)]:
            self.board[cy][cx] = self.turn
        self.turn *= -1
        self.wait = 0.35
        passed = ""
        if not self.legal(self.turn):
            self.turn *= -1
            passed = "Opponent passes. "
            if not self.legal(self.turn):
                self.over = True
                black = sum(row.count(1) for row in self.board)
                white = sum(row.count(-1) for row in self.board)
                result = (
                    "You win"
                    if black > white
                    else "CPU wins"
                    if white > black
                    else "Draw"
                )
                self.status = f"{result}: black {black}, white {white}"
                return True
        self.status = passed + (
            "Your turn: black" if self.turn == 1 else "CPU turn: white"
        )
        return True

    def handle(self, event):
        if (
            event.type == pygame.MOUSEBUTTONDOWN
            and event.button == 1
            and self.turn == 1
        ):
            x = (event.pos[0] - self.origin[0]) // self.cell
            y = (event.pos[1] - self.origin[1]) // self.cell
            self.play(x, y)

    def update(self, dt):
        if self.over or self.turn == 1:
            return
        self.wait -= dt
        if self.wait <= 0:
            moves = self.legal(-1)
            self.rng.shuffle(moves)
            move = max(
                moves,
                key=lambda p: (
                    len(self.flips(*p, -1))
                    + (30 if p[0] in (0, 7) and p[1] in (0, 7) else 0)
                ),
            )
            self.play(*move)

    def draw(self, surface):
        header(surface, "Othello / オセロ", self.status)
        legal = self.legal(1) if self.turn == 1 and not self.over else []
        for y in range(8):
            for x in range(8):
                rect = pygame.Rect(40 + x * 64, 115 + y * 64, 64, 64)
                pygame.draw.rect(surface, (27, 121, 86), rect)
                pygame.draw.rect(surface, (15, 63, 48), rect, 1)
                if self.board[y][x]:
                    color = (20, 22, 28) if self.board[y][x] == 1 else (242, 240, 226)
                    pygame.draw.circle(surface, color, rect.center, 26)
                elif (x, y) in legal:
                    pygame.draw.circle(surface, (111, 200, 151), rect.center, 6)
        text(surface, "You: black / CPU: white", 590, 130)
        text(surface, "Click a highlighted square.", 590, 175)
        text(surface, "No legal move: automatic pass.", 590, 215, 19)
        text(surface, "Most disks at the end wins.", 590, 250, 19)


class Gomoku:
    """Freestyle Gomoku: five or more in a row wins; no forbidden moves."""

    def __init__(self, seed=0):
        self.rng = random.Random(seed)
        self.board = [[0] * 15 for _ in range(15)]
        self.turn = 1
        self.over = False
        self.status = "Your turn: black"
        self.wait = 0.35

    def won(self, x, y, side):
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            count = 1
            for sign in (-1, 1):
                cx, cy = x + sign * dx, y + sign * dy
                while 0 <= cx < 15 and 0 <= cy < 15 and self.board[cy][cx] == side:
                    count += 1
                    cx, cy = cx + sign * dx, cy + sign * dy
            if count >= 5:
                return True
        return False

    def play(self, x, y):
        if self.over or not (0 <= x < 15 and 0 <= y < 15) or self.board[y][x]:
            return False
        self.board[y][x] = self.turn
        if self.won(x, y, self.turn):
            self.over = True
            self.status = (
                "You win: five in a row"
                if self.turn == 1
                else "CPU wins: five in a row"
            )
        elif all(all(row) for row in self.board):
            self.over = True
            self.status = "Draw: board full"
        else:
            self.turn *= -1
            self.wait = 0.35
            self.status = "Your turn: black" if self.turn == 1 else "CPU turn: white"
        return True

    def handle(self, event):
        if (
            event.type == pygame.MOUSEBUTTONDOWN
            and event.button == 1
            and self.turn == 1
        ):
            self.play(round((event.pos[0] - 55) / 38), round((event.pos[1] - 120) / 38))

    def score(self, x, y, side):
        self.board[y][x] = side
        if self.won(x, y, side):
            self.board[y][x] = 0
            return 1000000
        value = 0
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            count, opens = 1, 0
            for sign in (-1, 1):
                cx, cy = x + sign * dx, y + sign * dy
                while 0 <= cx < 15 and 0 <= cy < 15 and self.board[cy][cx] == side:
                    count += 1
                    cx, cy = cx + sign * dx, cy + sign * dy
                if 0 <= cx < 15 and 0 <= cy < 15 and self.board[cy][cx] == 0:
                    opens += 1
            value += (10**count) * opens
        self.board[y][x] = 0
        return value

    def update(self, dt):
        if self.over or self.turn == 1:
            return
        self.wait -= dt
        if self.wait > 0:
            return
        moves = [(x, y) for y in range(15) for x in range(15) if not self.board[y][x]]
        self.rng.shuffle(moves)
        move = max(
            moves,
            key=lambda p: (
                max(self.score(*p, -1) * 1.1, self.score(*p, 1))
                - (abs(p[0] - 7) + abs(p[1] - 7))
            ),
        )
        self.play(*move)

    def draw(self, surface):
        header(surface, "Gomoku / 五目並べ", self.status)
        pygame.draw.rect(surface, (183, 143, 88), (36, 101, 570, 570))
        for i in range(15):
            pygame.draw.line(
                surface, (66, 47, 30), (55 + i * 38, 120), (55 + i * 38, 652)
            )
            pygame.draw.line(
                surface, (66, 47, 30), (55, 120 + i * 38), (587, 120 + i * 38)
            )
        for y in range(15):
            for x in range(15):
                if self.board[y][x]:
                    color = (23, 24, 29) if self.board[y][x] == 1 else (244, 241, 230)
                    pygame.draw.circle(surface, color, (55 + x * 38, 120 + y * 38), 16)
        text(surface, "You: black / CPU: white", 630, 130)
        text(surface, "Click an intersection.", 630, 175)
        text(surface, "Five or more in a row wins.", 630, 220, 19)
        text(surface, "Freestyle: no forbidden moves.", 630, 255, 18)


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
                        self.labels[piece.piece_type],
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
                f"{self.labels[piece_type]} {count}",
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
            f"{self.labels[p]}{n}"
            for p, n in sorted(self.board.pieces_in_hand[shogi.WHITE].items())
            if n
        )
        text(surface, "CPU hand: " + (cpu_hand or "none"), 610, 610, 18)
