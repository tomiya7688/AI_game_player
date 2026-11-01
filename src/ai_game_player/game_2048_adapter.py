from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import re
from math import isfinite
from typing import Any

from ai_game_player.capture_region import PixelRect
from ai_game_player.game_2048_board import (
    BOARD_DIMENSION,
    MINIMUM_BOARD_CONFIDENCE,
    BoardRegion,
    Game2048BoardRecognizer,
)
from ai_game_player.models import ActionCandidate, ScreenObservation
from ai_game_player.screen_capture import ScreenFrame


ARROW_DIRECTIONS = ("UP", "DOWN", "LEFT", "RIGHT")
SCORE_PANEL_SEARCH_HEIGHT_FRACTION = 0.5
OCR_TEXT_FALLBACK_CONFIDENCE = 0.5


class ReferenceGame2048Adapter:
    # {
    #   責務: [ReferenceGame2048Adapter: Observationと対応するraw frameから2048 stateと確信度条件を通過したArrow key候補を生成する]
    #   フィールド: [board_recognizer: 公式CSS色による4x4認識器, minimum_candidate_confidence: 候補生成を許可する最小状態確信度]
    # }
    def __init__(
        self,
        board_recognizer: Game2048BoardRecognizer | None = None,
        minimum_candidate_confidence: float = MINIMUM_BOARD_CONFIDENCE,
    ) -> None:
        # {
        #   責務: [__init__: board認識器とkeyboard候補の最低確信度を設定する]
        #   引数: [board_recognizer: 差替え可能な2048 board認識器, minimum_candidate_confidence: 4方向候補を許す最小状態確信度]
        #   戻り値: []
        #   エラー: [閾値が有限値でないか0から1の範囲外: 不安全な候補設定をValueErrorで拒否する]
        # }
        if not isfinite(minimum_candidate_confidence) or not 0 <= minimum_candidate_confidence <= 1:
            raise ValueError("minimum candidate confidence must be between 0 and 1")
        self.board_recognizer = board_recognizer or Game2048BoardRecognizer()
        self.minimum_candidate_confidence = minimum_candidate_confidence

    # {
    #   責務: [adapt: DOMやgame内部stateを使わず、screen frameと同じframe由来Observationから盤面・score・終了状態・key候補を作る]
    #   処理: [boardを認識し、OCR scoreとlegal-move条件を合わせたstate signatureを作成する。盤面が低確信度またはgame-overならkey候補を出さない]
    #   引数: [frame: Observation生成元と同じcapture時点のBGRA画面, observation: OCRなど同じframeから抽出済みの画面特徴]
    #   戻り値: [tuple[ScreenObservation, list[ActionCandidate]]: game_2048 stateを追加したObservationと4方向keyboard候補]
    #   エラー: [raw frameの寸法不一致: BoardRecognizerのValueErrorを呼び出し元へ送出]
    # }
    def adapt(
        self,
        frame: ScreenFrame,
        observation: ScreenObservation,
    ) -> tuple[ScreenObservation, list[ActionCandidate]]:
        board = self.board_recognizer.recognize(frame)
        coordinate_bounds = _board_bounds_in_observation_coordinates(frame, board.region) if board else None
        score, score_confidence = _read_score(observation, coordinate_bounds)
        game_over = _is_game_over(observation, board.cells if board else None)
        state_signature = _state_signature(board.cells, score, game_over) if board else None
        # Empty cells can be certain pixels while this nonterminal screen is not ready.
        nonterminal_empty = board is not None and not game_over and all(
            value == 0 for row in board.cells for value in row
        )
        state_available = (
            board is not None
            and board.confidence >= self.minimum_candidate_confidence
            and not nonterminal_empty
            and not any(value is None for row in board.cells for value in row)
        )
        detected_game_over: bool | None = game_over if board is not None or game_over else None
        game_state: dict[str, Any] = {
            "board": [list(row) for row in board.cells] if board else None,
            "board_bbox": coordinate_bounds,
            "cell_confidence": [list(row) for row in board.cell_confidence] if board else None,
            "confidence": board.confidence if board else 0.0,
            "game_over": detected_game_over,
            "score": score,
            "score_confidence": score_confidence,
            "state_signature": state_signature,
            "state_available": state_available,
            "unavailable_reason": "empty_nonterminal" if nonterminal_empty else None,
        }
        features = dict(observation.features)
        features["game_2048"] = game_state
        adapted_observation = replace(observation, features=features)
        if not state_available or game_over:
            return adapted_observation, []
        assert board is not None
        candidates = [
            ActionCandidate(f"2048-move-{direction.lower()}", "key", direction, confidence=board.confidence)
            for direction in ARROW_DIRECTIONS
        ]
        return adapted_observation, candidates


def _board_bounds_in_observation_coordinates(
    frame: ScreenFrame,
    region: BoardRegion,
) -> tuple[int, int, int, int]:
    # {
    #   責務: [_board_bounds_in_observation_coordinates: ROI内のboard矩形をObservationとOCR候補が使うscreen/window座標へ変換する]
    #   引数: [frame: ROI geometryを保持するcapture frame, region: ROI内で認識したboard矩形]
    #   戻り値: [tuple[int, int, int, int]: Observation座標系のx/y/width/height]
    # }
    if frame.capture_geometry is None:
        return region.x, region.y, region.width, region.height
    mapped = frame.capture_geometry.roi_rect_to_coordinates(
        PixelRect(region.x, region.y, region.width, region.height)
    )
    return mapped.x, mapped.y, mapped.width, mapped.height


def _candidate_bbox(candidate: dict[str, Any]) -> tuple[float, float, float, float] | None:
    # {
    #   責務: [_candidate_bbox: OCR candidateからscore labelと数字の位置を比較できる矩形を読む]
    #   引数: [candidate: OCR providerが出力するtext/confidence/bboxまたはx/y/width/height辞書]
    #   戻り値: [tuple[float, float, float, float]またはNone: OCR候補のx/y/width/height]
    # }
    raw_bbox = candidate.get("bbox")
    if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
        try:
            x, y, width, height = (float(value) for value in raw_bbox)
        except (OverflowError, TypeError, ValueError):
            return None
        bbox = (x, y, width, height)
        if not all(isfinite(value) for value in bbox) or width <= 0 or height <= 0:
            return None
        return bbox
    try:
        x, y, width, height = (
            float(candidate[key]) for key in ("x", "y", "width", "height")
        )
    except (KeyError, OverflowError, TypeError, ValueError):
        return None
    bbox = (x, y, width, height)
    if not all(isfinite(value) for value in bbox) or width <= 0 or height <= 0:
        return None
    return bbox


def _score_numbers(text: str) -> list[int]:
    # {
    #   責務: [_score_numbers: OCR文字列から2048の非負整数score候補だけを抽出する]
    #   引数: [text: SCORE表示やカンマ区切りscoreを含むOCR文字列]
    #   戻り値: [list[int]: OCR文字列内にあるscore数値を出現順に返す]
    # }
    numbers = []
    for match in re.findall(r"\d[\d,]*", text):
        try:
            numbers.append(int(match.replace(",", "")))
        except ValueError:
            continue
    return numbers


def _read_score(
    observation: ScreenObservation,
    board_bounds: tuple[int, int, int, int] | None,
) -> tuple[int | None, float]:
    # {
    #   責務: [_read_score: OCR Observationから現在scoreをBEST scoreと区別して取得する]
    #   処理: [SCORE labelに最も近い数字を優先し、label位置がない場合はboard上部の左側score欄を使う]
    #   引数: [observation: 同じcapture frameのOCR candidates/text, board_bounds: OCR座標系の2048 board矩形]
    #   戻り値: [tuple[int | None, float]: 読み取った現在scoreとOCR confidence。数字なしならNoneと0]
    # }
    raw_candidates = observation.features.get("ocr_candidates", [])
    candidates = (
        [item for item in raw_candidates if isinstance(item, dict)]
        if isinstance(raw_candidates, list)
        else []
    )
    numeric_candidates: list[tuple[int, float, tuple[float, float, float, float] | None, str]] = []
    score_labels: list[tuple[float, float, float, float]] = []
    for candidate in candidates:
        text = str(candidate.get("text", candidate.get("label", "")))
        lowered = text.casefold()
        confidence_value = candidate.get("confidence", 0.0)
        try:
            confidence = float(confidence_value)
        except (TypeError, ValueError):
            confidence = 0.0
        if not isfinite(confidence) or not 0 <= confidence <= 1:
            confidence = 0.0
        bbox = _candidate_bbox(candidate)
        if "score" in lowered and bbox is not None:
            score_labels.append(bbox)
        for value in _score_numbers(text):
            numeric_candidates.append((value, confidence, bbox, lowered))

    if score_labels:
        labeled_numbers = [candidate for candidate in numeric_candidates if "score" in candidate[3]]
        if labeled_numbers:
            value, confidence, _, _ = labeled_numbers[0]
            return value, confidence
        located_numbers: list[tuple[int, float, tuple[float, float, float, float], str]] = []
        for value, confidence, bbox, text in numeric_candidates:
            if bbox is not None:
                located_numbers.append((value, confidence, bbox, text))
        if located_numbers:
            label = score_labels[0]
            label_center = (label[0] + label[2] / 2, label[1] + label[3] / 2)
            nearest = min(
                located_numbers,
                key=lambda candidate: (
                    ((candidate[2][0] + candidate[2][2] / 2 - label_center[0]) ** 2)
                    + ((candidate[2][1] + candidate[2][3] / 2 - label_center[1]) ** 2)
                ),
            )
            return nearest[0], nearest[1]

    if board_bounds is not None:
        board_x, board_y, board_width, _ = board_bounds
        above_board: list[tuple[int, float, tuple[float, float, float, float], str]] = []
        for value, confidence, bbox, text in numeric_candidates:
            if bbox is None:
                continue
            bbox_center_x = bbox[0] + bbox[2] / 2
            bbox_center_y = bbox[1] + bbox[3] / 2
            if (
                board_y - board_width * SCORE_PANEL_SEARCH_HEIGHT_FRACTION <= bbox_center_y < board_y
                and board_x <= bbox_center_x <= board_x + board_width
            ):
                above_board.append((value, confidence, bbox, text))
        if above_board:
            current_score = min(above_board, key=lambda candidate: candidate[2][0])
            return current_score[0], current_score[1]
    if not numeric_candidates:
        return _score_from_ocr_text(observation.ocr_text)
    if not any(candidate[2] is not None for candidate in numeric_candidates):
        return _score_from_ocr_text(observation.ocr_text)
    return None, 0.0


def _score_from_ocr_text(ocr_text: list[str]) -> tuple[int | None, float]:
    # {
    #   責務: [_score_from_ocr_text: bboxを持たないOCR列からSCOREとBESTの間にある数字を取得する]
    #   引数: [ocr_text: FrameAnalyzerがObservationへ格納したOCR文字列の順序付き一覧]
    #   戻り値: [tuple[int | None, float]: SCORE label直後の数値と中程度のconfidence。label不在ならNoneと0]
    # }
    score_label_seen = False
    for text in ocr_text:
        lowered = text.casefold()
        if "best" in lowered:
            break
        if "score" in lowered:
            score_label_seen = True
        if score_label_seen:
            values = _score_numbers(text)
            if values:
                return values[0], OCR_TEXT_FALLBACK_CONFIDENCE
    return None, 0.0


def _is_game_over(
    observation: ScreenObservation,
    board: tuple[tuple[int | None, ...], ...] | None,
) -> bool:
    # {
    #   責務: [_is_game_over: 明示的なGame Over文字列または2048の合法move不存在から終了状態を判定する]
    #   引数: [observation: frameから得たOCR text, board: 認識済み4x4盤面。未認識ならNone]
    #   戻り値: [bool: OCRまたは全cell状態が一致した場合だけgame-overをtrueにする]
    # }
    text_candidates = list(observation.ocr_text)
    raw_candidates = observation.features.get("ocr_candidates", [])
    if isinstance(raw_candidates, list):
        text_candidates.extend(
            str(candidate.get("text", ""))
            for candidate in raw_candidates
            if isinstance(candidate, dict)
        )
    normalized_text = " ".join(" ".join(text_candidates).casefold().split())
    if "game over" in normalized_text or "gameover" in normalized_text:
        return True
    if board is None or any(value is None for row in board for value in row):
        return False
    if any(value == 0 for row in board for value in row):
        return False
    for row in range(BOARD_DIMENSION):
        for column in range(BOARD_DIMENSION):
            value = board[row][column]
            if column < 3 and value == board[row][column + 1]:
                return False
            if row < 3 and value == board[row + 1][column]:
                return False
    return True


def _state_signature(
    board: tuple[tuple[int | None, ...], ...],
    score: int | None,
    game_over: bool,
) -> str:
    # {
    #   責務: [_state_signature: scoreと4x4盤面と終了状態を正規JSON化し、再現可能なSHA-256を計算する]
    #   引数: [board: screenから認識した4x4数値, score: OCRが読めた現在得点, game_over: 合法moveがないか終了表示を検出した状態]
    #   戻り値: [str: 同じgame stateが同じ値になる小文字hex SHA-256]
    # }
    payload = json.dumps(
        {"board": board, "game_over": game_over, "score": score},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()
