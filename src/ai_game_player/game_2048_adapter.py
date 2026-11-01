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
OCR_TEXT_FALLBACK_CONFIDENCE = 0.5
SCORE_VALUE_PATTERN = re.compile(
    r"\s*SCORE\s*[:：]?\s*([0-9]+(?:,[0-9]{3})*)\s*", re.IGNORECASE
)
SCORE_LABEL_MINIMUM_VERTICAL_FRACTION = 0.75
SCORE_LABEL_MAXIMUM_GAP_HEIGHTS = 2


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
    #   処理: [boardを認識し、OCR scoreとlegal-move条件を合わせたstate signatureを作成する。盤面が低確信度・未知cellを含む・game-overの場合はkey候補を出さない]
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
        }
        features = dict(observation.features)
        features["game_2048"] = game_state
        adapted_observation = replace(observation, features=features)
        if (
            board is None
            or board.confidence < self.minimum_candidate_confidence
            or game_over
            or any(value is None for row in board.cells for value in row)
        ):
            return adapted_observation, []
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
    #   処理: [SCORE labelの直下で水平に重なる数値だけを組にし、複数の異なる数値はunknownとする]
    #   引数: [observation: 同じcapture frameのOCR candidates/text, board_bounds: OCR座標系の2048 board矩形]
    #   戻り値: [tuple[int | None, float]: 読み取った現在scoreとOCR confidence。位置対応または明示文字列の根拠がなければNoneと0]
    # }
    raw_candidates = observation.features.get("ocr_candidates", [])
    candidates = (
        [item for item in raw_candidates if isinstance(item, dict)]
        if isinstance(raw_candidates, list)
        else []
    )
    numeric_candidates: list[
        tuple[int, float, tuple[float, float, float, float] | None, str]
    ] = []
    score_labels: list[tuple[tuple[float, float, float, float], float]] = []
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
        if (
            lowered.strip() == "score" or SCORE_VALUE_PATTERN.fullmatch(text)
        ) and bbox is not None:
            score_labels.append((bbox, confidence))
        for value in _score_numbers(text):
            numeric_candidates.append((value, confidence, bbox, lowered))

    readings: list[tuple[int, float]] = []
    for label, label_confidence in score_labels:
        if label_confidence <= 0 or (
            board_bounds is not None and label[1] + label[3] > board_bounds[1]
        ):
            continue
        label_center_x = label[0] + label[2] / 2
        matches: list[tuple[int, float]] = []
        for value, confidence, bbox, text in numeric_candidates:
            if "best" in text or confidence <= 0:
                continue
            if "score" in text and bbox == label and len(_score_numbers(text)) == 1:
                matches.append((value, min(confidence, label_confidence)))
                continue
            if bbox is None:
                continue
            # A header or board number is not evidence for the labelled panel.
            center_x = bbox[0] + bbox[2] / 2
            below_label = (
                bbox[1] >= label[1] + label[3] * SCORE_LABEL_MINIMUM_VERTICAL_FRACTION
            )
            near_label = bbox[1] - (
                label[1] + label[3]
            ) <= SCORE_LABEL_MAXIMUM_GAP_HEIGHTS * max(label[3], bbox[3])
            horizontal_overlap = (
                abs(center_x - label_center_x) <= (label[2] + bbox[2]) / 2
            )
            above_board = board_bounds is None or bbox[1] + bbox[3] <= board_bounds[1]
            if below_label and near_label and horizontal_overlap and above_board:
                matches.append((value, min(confidence, label_confidence)))
        if len({value for value, _ in matches}) == 1:
            readings.append(max(matches, key=lambda match: match[1]))
        elif matches:
            return None, 0.0
    if readings:
        if len({value for value, _ in readings}) == 1:
            return max(readings, key=lambda reading: reading[1])
        return None, 0.0
    if any(candidate[2] is not None for candidate in numeric_candidates):
        return None, 0.0
    return _score_from_ocr_text(observation.ocr_text)


def _score_from_ocr_text(ocr_text: list[str]) -> tuple[int | None, float]:
    # {
    #   責務: [_score_from_ocr_text: 位置なしOCRは同じ文字列内に明示されたSCORE数値だけを読む]
    #   処理: [別wordの順序や盤面上部の左端を得点の根拠にせず、曖昧ならunknownを保つ]
    #   引数: [ocr_text: 同じframeのOCR文字列群]
    #   戻り値: [tuple[int | None, float]: 一意な明示得点とfallback確信度、またはNoneと0]
    # }
    readings = []
    for text in ocr_text:
        matched = SCORE_VALUE_PATTERN.fullmatch(text)
        if matched:
            values = _score_numbers(matched.group(1))
            if len(values) == 1:
                readings.append(values[0])
    if len(set(readings)) == 1:
        return readings[0], OCR_TEXT_FALLBACK_CONFIDENCE
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
