"""評価専用: 同frameのuniform panelとOCR画素をOtsu/連結成分で分離する。"""

from collections import Counter, deque
from dataclasses import dataclass
import math

from ai_game_player.screen_capture import ScreenFrame


@dataclass(frozen=True)
class Panel:
    left: int
    top: int
    right: int
    bottom: int


def _gray(frame: ScreenFrame, box: tuple[int, int, int, int]) -> list[int]:
    left, top, right, bottom = box
    if not (0 <= left < right <= frame.width and 0 <= top < bottom <= frame.height):
        raise ValueError("score panel crop is out of bounds")
    if (right - left) * (bottom - top) > 100_000:
        raise ValueError("score panel pixel budget exceeded")
    return [
        (
            frame.bgra[(y * frame.width + x) * 4 + 2] * 299
            + frame.bgra[(y * frame.width + x) * 4 + 1] * 587
            + frame.bgra[(y * frame.width + x) * 4] * 114
            + 500
        )
        // 1000
        for y in range(top, bottom)
        for x in range(left, right)
    ]


def _otsu(pixels: list[int]) -> int:
    """256-bin Otsuのクラス間分散を最大化。単色は分離不能として拒否。"""
    histogram = [0] * 256
    for value in pixels:
        histogram[value] += 1
    total = len(pixels)
    moment = sum(i * n for i, n in enumerate(histogram))
    count = subtotal = 0
    best = -1.0
    threshold = None
    for value, frequency in enumerate(histogram):
        count += frequency
        subtotal += value * frequency
        if count == 0 or count == total:
            continue
        variance = (subtotal * total - moment * count) ** 2 / (count * (total - count))
        if variance > best:
            best, threshold = variance, value
    if threshold is None:
        raise ValueError("score panel has no separable contrast")
    return threshold


def find_panels(frame: ScreenFrame, box: tuple[int, int, int, int]) -> list[Panel]:
    """暗いuniform背景の4近傍component。ROI端に接する不完全panelは使わない。"""
    pixels = _gray(frame, box)
    threshold = _otsu(pixels)
    width, height = box[2] - box[0], box[3] - box[1]
    seen = bytearray(len(pixels))
    panels = []
    for start, value in enumerate(pixels):
        if value > threshold or seen[start]:
            continue
        todo = deque([start])
        seen[start] = 1
        left = right = start % width
        top = bottom = start // width
        count = 0
        while todo:
            index = todo.popleft()
            x, y = index % width, index // width
            left, right, top, bottom = (
                min(left, x),
                max(right, x),
                min(top, y),
                max(bottom, y),
            )
            count += 1
            for neighbor in (
                index - 1 if x else -1,
                index + 1 if x + 1 < width else -1,
                index - width if y else -1,
                index + width if y + 1 < height else -1,
            ):
                if (
                    neighbor >= 0
                    and not seen[neighbor]
                    and pixels[neighbor] <= threshold
                ):
                    seen[neighbor] = 1
                    todo.append(neighbor)
        w, h = right - left + 1, bottom - top + 1
        # A supported background occupies the majority of its rectangle. Individual
        # glyphs, open crop edges and very small components cannot anchor a panel.
        if (
            min(w, h) >= 8
            and w > h
            and count * 2 > w * h
            and left > 0
            and top > 0
            and right < width - 1
            and bottom < height - 1
        ):
            panels.append(
                Panel(
                    box[0] + left, box[1] + top, box[0] + right + 1, box[1] + bottom + 1
                )
            )
    if len(panels) > 4:
        raise ValueError("score panel component budget exceeded")
    return panels


def panel_for_label(
    frame: ScreenFrame, label_box: tuple[float, float, float, float]
) -> Panel:
    x, y, width, height = label_box
    box = (
        max(0, math.floor(x - width * 4)),
        max(0, math.floor(y - height * 2)),
        min(frame.width, math.ceil(x + width * 5)),
        min(frame.height, math.ceil(y + height * 6)),
    )
    panels = find_panels(frame, box)
    matches = [
        p
        for p in panels
        if p.left < x and p.top < y and p.right > x + width and p.bottom > y + height
    ]
    if len(matches) != 1:
        raise ValueError("score label does not belong to a unique supported panel")
    return matches[0]


def _numeric_support(
    frame: ScreenFrame, box: tuple[int, int, int, int], panel: Panel
) -> tuple[tuple[int, int, int, int], list[int], int]:
    left, top = max(box[0], panel.left + 2), max(box[1], panel.top + 2)
    right, bottom = min(box[2], panel.right - 2), min(box[3], panel.bottom - 2)
    ink = _gray(frame, (left, top, right, bottom))
    ink_threshold = _otsu(ink)
    ink_width, ink_height = right - left, bottom - top
    if any(
        value > ink_threshold
        and (x == 0 or x == ink_width - 1 or y == 0 or y == ink_height - 1)
        for y in range(ink_height)
        for x in range(ink_width)
        for value in (ink[y * ink_width + x],)
    ):
        raise ValueError("score foreground touches source support boundary")
    return (left, top, right, bottom), ink, ink_threshold


def numeric_crops(
    frame: ScreenFrame, panel: Panel, top: int, bottom: int
) -> list[tuple[int, int, int, int]]:
    """全foreground unionを保持し、余白比の違う実2cropをpanel内だけで作る。"""
    support, pixels, threshold = _numeric_support(
        frame, (panel.left, top, panel.right, bottom), panel
    )
    left, top, right, bottom = support
    width = right - left
    points = [
        (i % width, i // width) for i, value in enumerate(pixels) if value > threshold
    ]
    if not points:
        raise ValueError("score numeric foreground is unavailable")
    min_x, max_x = min(x for x, y in points), max(x for x, y in points)
    min_y, max_y = min(y for x, y in points), max(y for x, y in points)
    margin = max(2, math.ceil((max_y - min_y + 1) / 4))
    crops = [
        (
            max(left, left + min_x - padding),
            max(top, top + min_y - margin),
            min(right, left + max_x + 1 + padding),
            min(bottom, top + max_y + 1 + margin),
        )
        for padding in (margin, margin * 2)
    ]
    if crops[0] == crops[1]:
        raise ValueError("score requires two distinct foreground crops")
    return crops


def prepare_panel_ocr(
    frame: ScreenFrame,
    box: tuple[int, int, int, int],
    panel: Panel,
    *,
    numeric: bool = False,
    binary: bool = True,
) -> tuple[ScreenFrame, int]:
    """3倍bilinear→Otsuまたは明示gray→白余白。panel外は白にする。"""
    pixels = _gray(frame, box)
    width, height = box[2] - box[0], box[3] - box[1]
    # Check real source support before masking/resampling/padding can hide clipping.
    # Two pixels exclude rounded panel corners; touching this supported interior
    # boundary is ambiguous and cannot become accepted OCR evidence.
    if numeric:
        _numeric_support(frame, box, panel)
        panel = Panel(panel.left + 1, panel.top + 1, panel.right - 1, panel.bottom - 1)
    # Require both supported panel background and visible glyph contrast.
    _otsu(pixels)
    background_histogram = [0] * 256
    for y in range(height):
        for x in range(width):
            if (
                panel.left < box[0] + x < panel.right - 1
                and panel.top < box[1] + y < panel.bottom - 1
            ):
                background_histogram[pixels[y * width + x]] += 1
    if not any(background_histogram):
        raise ValueError("score panel contains no supported background")
    background = max(range(256), key=background_histogram.__getitem__)
    scaled = []
    for y in range(height * 3):
        fy = max(0.0, min(height - 1.0, (y + 0.5) / 3 - 0.5))
        y0, y1 = math.floor(fy), min(height - 1, math.floor(fy) + 1)
        wy = fy - y0
        for x in range(width * 3):
            fx = max(0.0, min(width - 1.0, (x + 0.5) / 3 - 0.5))
            x0, x1 = math.floor(fx), min(width - 1, math.floor(fx) + 1)
            wx = fx - x0
            value = round(
                (pixels[y0 * width + x0] * (1 - wx) + pixels[y0 * width + x1] * wx)
                * (1 - wy)
                + (pixels[y1 * width + x0] * (1 - wx) + pixels[y1 * width + x1] * wx)
                * wy
            )
            sx, sy = box[0] + fx, box[1] + fy
            # Mask outside this specific panel before deriving the Otsu threshold.
            scaled.append(
                value
                if panel.left < sx < panel.right - 1
                and panel.top < sy < panel.bottom - 1
                else background
            )
    threshold = _otsu(scaled)
    pad = 12
    out_width, out_height = width * 3 + pad * 2, height * 3 + pad * 2
    raw = bytearray(b"\xff\xff\xff\xff" * (out_width * out_height))
    if not binary:
        background = Counter(scaled).most_common(1)[0][0]
        peak = max(scaled)
        if peak <= background:
            raise ValueError("score grayscale contrast unavailable")
    for y in range(height * 3):
        for x in range(width * 3):
            value = scaled[y * width * 3 + x]
            if binary:
                value = 0 if value > threshold else 255
            else:
                value = min(
                    255, max(0, round(255 * (peak - value) / (peak - background)))
                )
            offset = ((y + pad) * out_width + x + pad) * 4
            raw[offset : offset + 4] = bytes((value, value, value, 255))
    return ScreenFrame(
        out_width, out_height, bytes(raw), captured_at=frame.captured_at
    ), pad
