"""Bounded, evaluation-only re-reading of a visible SCORE-labelled numeric line."""

from dataclasses import replace
import math
import re

from ai_game_player.screen_capture import ScreenFrame
from tools.planning_score_panel import find_panels, panel_for_label, prepare_panel_ocr


def _scaled_crop(frame, left, top, right, bottom):
    if (
        left < 0
        or top < 0
        or right > frame.width
        or bottom > frame.height
        or left >= right
        or top >= bottom
        or (right - left) * (bottom - top) * 9 > 1_000_000
    ):
        raise ValueError("score crop exceeds pixel bounds")
    rows = []
    for row in range(top, bottom):
        pixels = frame.bgra[
            (row * frame.width + left) * 4 : (row * frame.width + right) * 4
        ]
        expanded = b"".join(
            pixels[index : index + 4] * 3 for index in range(0, len(pixels), 4)
        )
        rows.append(expanded * 3)
    return ScreenFrame(
        (right - left) * 3,
        (bottom - top) * 3,
        b"".join(rows),
        captured_at=frame.captured_at,
    )


def reread_score_header_label(frame, observation, board_bounds, ocr):
    """Recover only the literal SCORE label from the board-relative pinned UI header."""
    if not isinstance(board_bounds, (list, tuple)) or len(board_bounds) != 4:
        return None, {"status": "score_header_board_unavailable"}
    x, y, width, height = board_bounds
    geometry = frame.capture_geometry
    if geometry is not None:
        convert = (
            geometry.window_to_roi
            if geometry.window_bounds is not None
            else geometry.screen_to_roi
        )
        right, bottom = convert(x + width, y + height)
        x, y = convert(x, y)
        width, height = right - x, bottom - y
    left, right = math.floor(x + width * 0.4), math.ceil(x + width)
    top, bottom = (
        max(0, math.floor(y - width * 0.45)),
        min(frame.height, math.ceil(y - width * 0.2)),
    )
    try:
        cropped = _scaled_crop(frame, left, top, right, bottom)
    except ValueError:
        return None, {"status": "score_header_crop_out_of_bounds"}
    words = ocr.recognize(cropped, mode="text_block")
    labels = []
    for word in words[:64]:
        if (
            word.get("text", "").casefold().strip() != "score"
            or not 0.65 <= word.get("confidence", 0) <= 1
        ):
            continue
        if (
            word["x"] <= 3
            or word["y"] <= 3
            or word["x"] + word["width"] >= cropped.width - 3
            or word["y"] + word["height"] >= cropped.height - 3
        ):
            continue
        mapped = dict(word)
        wx, wy = left + word["x"] / 3, top + word["y"] / 3
        ww, wh = word["width"] / 3, word["height"] / 3
        if geometry is not None:
            convert = (
                geometry.roi_to_window
                if geometry.window_bounds is not None
                else geometry.roi_to_screen
            )
            right_x, bottom_y = convert(wx + ww, wy + wh)
            wx, wy = convert(wx, wy)
            ww, wh = right_x - wx, bottom_y - wy
        mapped.update(x=wx, y=wy, width=ww, height=wh, bbox=(wx, wy, ww, wh))
        labels.append(mapped)
    diagnostic = {
        "status": "scaled_header_label",
        "crop_bbox": [left, top, right - left, bottom - top],
        "scale": 3,
        "label_count": len(labels),
        "label_confidences": [label["confidence"] for label in labels[:2]],
    }
    if not labels:
        try:
            panels = find_panels(frame, (left, top, right, bottom))
            for panel in panels:
                box = (panel.left, panel.top, panel.right, panel.bottom)
                prepared, pad = prepare_panel_ocr(frame, box, panel)
                for word in ocr.recognize(prepared, mode="text_block")[:64]:
                    if (
                        word.get("text", "").strip().casefold() != "score"
                        or not 0.65 <= word.get("confidence", 0) <= 1
                    ):
                        continue
                    mapped = dict(word)
                    wx, wy = (
                        panel.left + (word["x"] - pad) / 3,
                        panel.top + (word["y"] - pad) / 3,
                    )
                    ww, wh = word["width"] / 3, word["height"] / 3
                    if not (
                        panel.left < wx < wx + ww < panel.right
                        and panel.top < wy < wy + wh < panel.bottom
                    ):
                        continue
                    if geometry is not None:
                        convert = (
                            geometry.roi_to_window
                            if geometry.window_bounds is not None
                            else geometry.roi_to_screen
                        )
                        right_x, bottom_y = convert(wx + ww, wy + wh)
                        wx, wy = convert(wx, wy)
                        ww, wh = right_x - wx, bottom_y - wy
                    mapped.update(
                        x=wx, y=wy, width=ww, height=wh, bbox=(wx, wy, ww, wh)
                    )
                    labels.append(mapped)
            diagnostic.update(
                status="otsu_panel_header_label",
                panel_count=len(panels),
                label_count=len(labels),
                label_confidences=[label["confidence"] for label in labels[:2]],
            )
        except ValueError:
            diagnostic["panel_status"] = "unsupported_panel"
    if len(labels) != 1:
        return None, diagnostic
    return replace(
        observation,
        ocr_text=["SCORE"],
        features={**observation.features, "ocr_candidates": labels},
    ), diagnostic


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
        right, bottom = to_roi(x + width, y + height)
        x, y = to_roi(x, y)
        width, height = right - x, bottom - y
    try:
        panel = panel_for_label(frame, (x, y, width, height))
    except ValueError:
        panel = None
    center_x = x + width / 2
    top = math.floor(y + height)
    bottom = math.ceil(y + height * 5)
    if panel is not None:
        bottom = min(bottom, panel.bottom - 1)
    scale = 3
    readings = []
    crops = []
    half_width_base = (
        max(width, center_x - panel.left, panel.right - center_x)
        if panel is not None
        else width
    )
    for half_width in (half_width_base, half_width_base * 1.5):
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
        if [left, top, right - left, bottom - top] in crops:
            return None, {"status": "score_line_distinct_crops_unavailable"}
        pad = 0
        if panel is None:
            cropped = _scaled_crop(frame, left, top, right, bottom)
        else:
            try:
                cropped, pad = prepare_panel_ocr(
                    frame, (left, top, right, bottom), panel, numeric=True
                )
            except ValueError:
                return None, {"status": "score_panel_contrast_unavailable"}
        words = [
            dict(word) for word in ocr.recognize(cropped, mode="single_numeric_line")
        ]
        for word in words:
            word["x"] -= pad
            word["y"] -= pad
        if len(words) != 1 or not re.fullmatch(
            r"[0-9][0-9,]{0,11}", words[0].get("text", "")
        ):
            return None, {"status": "score_line_not_unique_numeric"}
        word = words[0]
        if (
            word["x"] <= scale
            or word["y"] <= scale
            or word["x"] + word["width"] >= cropped.width - pad * 2 - scale
            or word["y"] + word["height"] >= cropped.height - pad * 2 - scale
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
        right, bottom = to_coordinates(x + w, y + h)
        x, y = to_coordinates(x, y)
        w, h = right - x, bottom - y
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
        "preprocessing": "bilinear_otsu_supported_panel"
        if panel is not None
        else "nearest_raw",
    }
