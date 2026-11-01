"""Bounded, evaluation-only re-reading of a visible SCORE-labelled numeric line."""

from dataclasses import replace
import math
import re

from ai_game_player.screen_capture import ScreenFrame


def reread_score_line(frame, observation, ocr):
    """Use one confident label, never a board number or hidden game state."""
    labels = [
        item
        for item in observation.features.get("ocr_candidates", ())
        if str(item.get("text", "")).strip().casefold() == "score"
        and type(item.get("confidence")) in (int, float)
        and 0.65 <= item["confidence"] <= 1
    ]
    if len(labels) != 1:
        return None, {"status": "score_label_unavailable_or_ambiguous"}
    label = labels[0]
    box = label.get("bbox") or tuple(
        label.get(key) for key in ("x", "y", "width", "height")
    )
    if (
        not isinstance(box, (tuple, list))
        or len(box) != 4
        or any(
            type(value) not in (int, float) or not math.isfinite(value) for value in box
        )
    ):
        return None, {"status": "score_label_geometry_unavailable"}
    x, y, width, height = box
    geometry = frame.capture_geometry
    if geometry is not None:
        to_roi = (
            geometry.window_to_roi
            if geometry.window_bounds is not None
            else geometry.screen_to_roi
        )
        x, y = to_roi(x, y)
    center_x = x + width / 2
    top = math.floor(y + height)
    bottom = math.ceil(y + height * 5)
    scale = 3
    readings = []
    crops = []
    for half_width in (width, width * 1.5):
        left, right = (
            math.floor(center_x - half_width),
            math.ceil(center_x + half_width),
        )
        if (
            width <= 0
            or height <= 0
            or left < 0
            or right > frame.width
            or top < 0
            or bottom > frame.height
            or left >= right
            or top >= bottom
            or (right - left) * (bottom - top) * scale * scale > 1_000_000
        ):
            return None, {"status": "score_line_crop_out_of_bounds"}
        rows = []
        for row in range(top, bottom):
            pixels = frame.bgra[
                (row * frame.width + left) * 4 : (row * frame.width + right) * 4
            ]
            expanded = b"".join(
                pixels[index : index + 4] * scale for index in range(0, len(pixels), 4)
            )
            rows.append(expanded * scale)
        cropped = ScreenFrame(
            (right - left) * scale,
            (bottom - top) * scale,
            b"".join(rows),
            captured_at=frame.captured_at,
        )
        words = ocr.recognize(cropped, mode="single_numeric_line")
        if len(words) != 1 or not re.fullmatch(
            r"[0-9][0-9,]{0,11}", words[0].get("text", "")
        ):
            return None, {"status": "score_line_not_unique_numeric"}
        word = words[0]
        if (
            word["x"] <= scale
            or word["y"] <= scale
            or word["x"] + word["width"] >= cropped.width - scale
            or word["y"] + word["height"] >= cropped.height - scale
        ):
            return None, {"status": "score_line_touches_crop_edge"}
        readings.append((word, left))
        crops.append([left, top, right - left, bottom - top])
    if readings[0][0]["text"].replace(",", "") != readings[1][0]["text"].replace(
        ",", ""
    ):
        return None, {"status": "score_line_width_disagreement", "crop_bboxes": crops}
    word, left = readings[1]
    candidates = [dict(label)]
    for word in (word,):
        mapped = dict(word)
        mapped["confidence"] = min(reading[0]["confidence"] for reading in readings)
        x = left + word["x"] / scale
        y = top + word["y"] / scale
        w, h = word["width"] / scale, word["height"] / scale
        if geometry is not None:
            to_coordinates = (
                geometry.roi_to_window
                if geometry.window_bounds is not None
                else geometry.roi_to_screen
            )
            x, y = to_coordinates(x, y)
        mapped.update(x=x, y=y, width=w, height=h, bbox=(x, y, w, h))
        candidates.append(mapped)
    return replace(
        observation,
        ocr_text=[item["text"] for item in candidates],
        features={**observation.features, "ocr_candidates": candidates},
    ), {
        "status": "single_numeric_line_width_agreement",
        "crop_bboxes": crops,
        "scale": scale,
        "score_label_confidence": label["confidence"],
        "numeric_word_count": 1,
    }
