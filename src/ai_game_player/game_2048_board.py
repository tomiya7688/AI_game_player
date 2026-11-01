from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from math import isfinite
from statistics import median_low

from ai_game_player.screen_capture import ScreenFrame


FRAME_BYTES_PER_PIXEL = 4
BOARD_DIMENSION = 4
BOARD_CELL_COUNT = BOARD_DIMENSION * BOARD_DIMENSION
BOARD_BACKGROUND_RGB = (187, 173, 160)
EMPTY_CELL_RGB = (205, 192, 180)
OVERLAY_RGB = (238, 228, 218)
GAME_OVER_OVERLAY_ALPHA = 0.5
TILE_RGB_BY_VALUE = {
    2: (238, 228, 218),
    4: (237, 224, 200),
    8: (242, 177, 121),
    16: (245, 149, 99),
    32: (246, 124, 95),
    64: (246, 94, 59),
    128: (237, 207, 114),
    256: (237, 204, 97),
    512: (237, 200, 80),
    1024: (237, 197, 63),
    2048: (237, 194, 46),
}
BOARD_CENTER_FRACTIONS = (0.13625, 0.37875, 0.62125, 0.86375)
CELL_SIZE_FRACTION = 0.2125
CELL_PATCH_OFFSETS = (0.25, 0.75)
CELL_CENTER_FRACTION = 0.5
MINIMUM_BOARD_CONFIDENCE = 0.65
MINIMUM_BOARD_SIDE_PIXELS = 80
MAXIMUM_COLOR_DISTANCE_SQUARED = 36
MAXIMUM_BOARD_REGION_COLOR_DISTANCE_SQUARED = 324
MAXIMUM_BOARD_ROW_COLOR_GAP_PIXELS = 16
MINIMUM_ROW_MATCH_PIXELS = 24
MINIMUM_ROW_MATCH_WIDTH_FRACTION = 0.05
MINIMUM_ROW_SPAN_PIXELS = 50
MINIMUM_ROW_SPAN_WIDTH_FRACTION = 0.08
MAXIMUM_ROW_GROUP_Y_DELTA = 2
BOARD_OUTER_PADDING_FRACTION = 0.03
MINIMUM_BOARD_SIDE_FRAME_FRACTION = 0.15
MINIMUM_BOARD_SHAPE_CONFIDENCE = 0.78
MINIMUM_BOARD_COLOR_DENSITY = 0.25
FULL_BOARD_COLOR_DENSITY = 0.72
CELL_PATCH_NEIGHBOR_RADIUS = 1
MINIMUM_CELL_COLOR_CONFIDENCE = 0.6
MINIMUM_VALID_CELL_COLOR_SAMPLES = 4


def _blend_with_overlay(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    # {
    #   責務: [_blend_with_overlay: game-over overlayが半透明で重なる色をsRGBの8bit RGBへ合成する]
    #   引数: [rgb: overlay下に表示されるboardまたはtileのCSS RGB色]
    #   戻り値: [tuple[int, int, int]: overlay表示中にcaptureされる近似RGB色]
    # }
    return (
        round((1 - GAME_OVER_OVERLAY_ALPHA) * rgb[0] + GAME_OVER_OVERLAY_ALPHA * OVERLAY_RGB[0]),
        round((1 - GAME_OVER_OVERLAY_ALPHA) * rgb[1] + GAME_OVER_OVERLAY_ALPHA * OVERLAY_RGB[1]),
        round((1 - GAME_OVER_OVERLAY_ALPHA) * rgb[2] + GAME_OVER_OVERLAY_ALPHA * OVERLAY_RGB[2]),
    )


def _board_region_colors() -> frozenset[tuple[int, int, int]]:
    # {
    #   責務: [_board_region_colors: 2048ゲーム盤面を画素rowから探す色集合を返す]
    #   戻り値: [frozenset[tuple[int, int, int]]: 通常表示とgame-over overlayで盤面に現れるRGB色]
    # }
    normal_colors = {BOARD_BACKGROUND_RGB, EMPTY_CELL_RGB, *TILE_RGB_BY_VALUE.values()}
    overlay_colors = {_blend_with_overlay(color) for color in normal_colors}
    return frozenset(normal_colors | overlay_colors)


def _cell_value_colors() -> dict[tuple[int, int, int], int]:
    # {
    #   責務: [_cell_value_colors: 空cellとtileのCSS RGB色を盤面数値へ対応付ける]
    #   戻り値: [dict[tuple[int, int, int], int]: 2048盤面から検出する色とtile値の対応]
    # }
    colors = {EMPTY_CELL_RGB: 0}
    colors.update({rgb: value for value, rgb in TILE_RGB_BY_VALUE.items()})
    colors[_blend_with_overlay(EMPTY_CELL_RGB)] = 0
    colors.update(
        {_blend_with_overlay(rgb): value for value, rgb in TILE_RGB_BY_VALUE.items()}
    )
    return colors


BOARD_REGION_COLORS = _board_region_colors()
CELL_VALUE_BY_RGB = _cell_value_colors()


@lru_cache(maxsize=4096)
def _is_board_region_color(rgb: tuple[int, int, int]) -> bool:
    # {
    #   責務: [_is_board_region_color: capture時の色ずれを許容し、2048盤面に属するRGB画素を判定する]
    #   引数: [rgb: screen captureから読み取った画素のRGB色]
    #   戻り値: [bool: 盤面palette色とのRGB二乗距離が324以下ならTrue]
    # }
    if rgb in BOARD_REGION_COLORS:
        return True
    return any(
        sum((actual - expected) ** 2 for actual, expected in zip(rgb, palette_rgb))
        <= MAXIMUM_BOARD_REGION_COLOR_DISTANCE_SQUARED
        for palette_rgb in BOARD_REGION_COLORS
    )


# {
#   責務: [group_board_color_rows: 縦方向に並ぶ盤面色の帯を、短い検出欠落だけを橋渡ししてgroupへ分ける]
#   処理: [行間距離を2 pixelまたは帯幅の3%まで許容する。3%は固定版CSSの盤面余白15/500である。2 pixelを超える欠落では先頭帯と重なり左右どちらかの端が同じ余白内で一致する帯だけを接続する。短い欠落中に現れる先頭帯の内側の断片は採用せず、最後に採用した行から余白以内で全幅帯が戻る場合だけ橋渡しする。内部断片だけで橋渡し可能な距離を延長しない]
#   引数: [matched_rows: y昇順のtuple群。各tupleはy、左端x、右端x（範囲外）、palette一致pixel数]
#   戻り値: [list: 同じ盤面候補として縦横比・密度を検査する行group。元のtupleと入力listは変更しない]
# }
def group_board_color_rows(
    matched_rows: list[tuple[int, int, int, int]],
) -> list[list[tuple[int, int, int, int]]]:
    row_groups: list[list[tuple[int, int, int, int]]] = []
    for row in matched_rows:
        maximum_y_delta = MAXIMUM_ROW_GROUP_Y_DELTA
        aligned_across_gap = True
        if row_groups:
            previous_row = row_groups[-1][-1]
            reference_row = row_groups[-1][0]
            nearby_span_width = max(row[2] - row[1], reference_row[2] - reference_row[1])
            maximum_y_delta = max(
                maximum_y_delta, round(nearby_span_width * BOARD_OUTER_PADDING_FRACTION)
            )
            if row[0] - previous_row[0] > MAXIMUM_ROW_GROUP_Y_DELTA:
                overlap = min(row[2], reference_row[2]) - max(row[1], reference_row[1])
                edge_distance = min(abs(row[1] - reference_row[1]), abs(row[2] - reference_row[2]))
                aligned_across_gap = overlap > 0 and edge_distance <= maximum_y_delta
                contained_fragment = row[1] >= reference_row[1] and row[2] <= reference_row[2]
                if (
                    not aligned_across_gap
                    and contained_fragment
                    and row[0] - previous_row[0] <= maximum_y_delta
                ):
                    continue
        if row_groups and aligned_across_gap and row[0] <= row_groups[-1][-1][0] + maximum_y_delta:
            row_groups[-1].append(row)
        else:
            row_groups.append([row])
    return row_groups


# {
#   責務: [_robust_board_bounds: 局所的な発光や短い内部断片を矩形の外端に採用しない]
#   処理: [行の左右端の中央値を推定し、少なくとも片辺が盤面余白内で一致し盤面幅の過半数を覆う行から上下端を決める]
#   引数: [group: 連続した盤面候補のy・左端・右端・一致画素数]
#   戻り値: [tupleまたはNone: 観測で支持された左・上・右・下（右/下は範囲外）]
# }
def _robust_board_bounds(
    group: list[tuple[int, int, int, int]],
) -> tuple[int, int, int, int] | None:
    if not group:
        return None
    left = median_low(row[1] for row in group)
    right = median_low(row[2] for row in group)
    tolerance = max(
        MAXIMUM_ROW_GROUP_Y_DELTA, round((right - left) * BOARD_OUTER_PADDING_FRACTION)
    )
    aligned = [
        row
        for row in group
        if (abs(row[1] - left) <= tolerance or abs(row[2] - right) <= tolerance)
        and min(right, row[2]) - max(left, row[1]) > (right - left) / 2
    ]
    if not aligned:
        return None
    return left, aligned[0][0], right, aligned[-1][0] + 1


@dataclass(frozen=True)
class BoardRegion:
    # {
    #   責務: [BoardRegion: raw capture frame内で検出した2048盤面の矩形と形状確信度を保持する]
    #   フィールド: [x/y: frame内左上pixel, width/height: frame内の矩形寸法, confidence: 正方形領域としての検出確信度]
    # }
    x: int
    y: int
    width: int
    height: int
    confidence: float


@dataclass(frozen=True)
class BoardRecognition:
    # {
    #   責務: [BoardRecognition: 認識した4x4盤面、元frame内矩形、各cellと全体の確信度を保持する]
    #   フィールド: [region: 盤面矩形, cells: 空cellは0、未知cellはNoneで表す4x4値, cell_confidence: 各tile判定の確信度, confidence: geometryとtile判定を合わせた値]
    # }
    region: BoardRegion
    cells: tuple[tuple[int | None, ...], ...]
    cell_confidence: tuple[tuple[float, ...], ...]
    confidence: float


class Game2048BoardRecognizer:
    # {
    #   責務: [Game2048BoardRecognizer: 2048公式CSS色と4x4配置を使い、raw screen pixelsから盤面を復元する]
    #   フィールド: [minimum_confidence: tile認識が候補生成を許可する最小確信度]
    # }
    def __init__(self, minimum_confidence: float = MINIMUM_BOARD_CONFIDENCE) -> None:
        # {
        #   責務: [__init__: 低確信度盤面を操作候補へ渡さない閾値を設定する]
        #   引数: [minimum_confidence: 0から1の範囲で候補生成を許可する最小盤面確信度]
        #   戻り値: []
        #   エラー: [閾値が有限値でないか0から1の範囲外: 設定誤りをValueErrorで通知する]
        # }
        if not isfinite(minimum_confidence) or not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum board confidence must be between 0 and 1")
        self.minimum_confidence = minimum_confidence

    # {
    #   責務: [recognize: game UIを参照せず、BGRA capture frameだけから2048盤面を認識する]
    #   処理: [色rowの密度から正方形board領域を探し、16 cellの背景色をCSS paletteへ照合する]
    #   引数: [frame: OS captureが返したwidth・height・BGRA byte列を持つ画面]
    #   戻り値: [BoardRecognitionまたはNone: 信頼できる4x4盤面、または候補を作れない認識結果]
    #   エラー: [frame寸法とBGRA byte数が一致しない場合: 不完全画像を解析せずValueErrorを送出する]
    # }
    def recognize(self, frame: ScreenFrame) -> BoardRecognition | None:
        if frame.width <= 0 or frame.height <= 0:
            raise ValueError("frame dimensions must be positive")
        if len(frame.bgra) != frame.width * frame.height * FRAME_BYTES_PER_PIXEL:
            raise ValueError("BGRA buffer size does not match frame dimensions")
        region = self._find_board_region(frame)
        if region is None or region.confidence < self.minimum_confidence:
            return None
        cells: list[tuple[int | None, ...]] = []
        cell_confidence: list[tuple[float, ...]] = []
        for row in range(BOARD_DIMENSION):
            row_values: list[int | None] = []
            row_confidence: list[float] = []
            for column in range(BOARD_DIMENSION):
                value, confidence = self._read_cell(frame, region, row, column)
                row_values.append(value)
                row_confidence.append(confidence)
            cells.append(tuple(row_values))
            cell_confidence.append(tuple(row_confidence))
        average_cell_confidence = sum(sum(row) for row in cell_confidence) / BOARD_CELL_COUNT
        return BoardRecognition(
            region,
            tuple(cells),
            tuple(cell_confidence),
            round(region.confidence * average_cell_confidence, 6),
        )

    # {
    #   責務: [_find_board_region: row内の2048 CSS色分布から最大の正方形board候補を特定する]
    #   処理: [paletteとのRGB二乗距離324以内のpixelをrow内gap16で帯状にまとめる。縦方向は盤面余白相当の短い欠落だけを橋渡しし、行端点の中央値と幅過半数・片辺の整合から外れ値を除いた矩形を作り、縦横比と画素密度で検査する]
    #   引数: [frame: 検索対象となるBGRA画面]
    #   戻り値: [BoardRegionまたはNone: 条件を満たす盤面矩形と形状確信度]
    # }
    def _find_board_region(self, frame: ScreenFrame) -> BoardRegion | None:
        minimum_row_pixels = max(
            MINIMUM_ROW_MATCH_PIXELS,
            round(frame.width * MINIMUM_ROW_MATCH_WIDTH_FRACTION),
        )
        minimum_row_span = max(
            MINIMUM_ROW_SPAN_PIXELS,
            round(frame.width * MINIMUM_ROW_SPAN_WIDTH_FRACTION),
        )
        matched_rows: list[tuple[int, int, int, int]] = []
        for y in range(frame.height):
            row_offset = y * frame.width * FRAME_BYTES_PER_PIXEL
            matching_x_positions: list[int] = []
            for x in range(frame.width):
                pixel_offset = row_offset + x * FRAME_BYTES_PER_PIXEL
                rgb = (
                    frame.bgra[pixel_offset + 2],
                    frame.bgra[pixel_offset + 1],
                    frame.bgra[pixel_offset],
                )
                if not _is_board_region_color(rgb):
                    continue
                matching_x_positions.append(x)
            if not matching_x_positions:
                continue
            row_color_spans: list[tuple[int, int, int]] = []
            span_left = matching_x_positions[0]
            span_right = span_left
            span_pixel_count = 1
            for x in matching_x_positions[1:]:
                if x - span_right - 1 > MAXIMUM_BOARD_ROW_COLOR_GAP_PIXELS:
                    row_color_spans.append((span_left, span_right + 1, span_pixel_count))
                    span_left = x
                    span_pixel_count = 0
                span_right = x
                span_pixel_count += 1
            row_color_spans.append((span_left, span_right + 1, span_pixel_count))
            left, right, matching_pixels = max(
                row_color_spans,
                key=lambda span: (span[2], span[1] - span[0]),
            )
            if matching_pixels >= minimum_row_pixels and right - left >= minimum_row_span:
                matched_rows.append((y, left, right, matching_pixels))

        row_groups = group_board_color_rows(matched_rows)

        candidates: list[tuple[float, BoardRegion]] = []
        minimum_side = max(
            MINIMUM_BOARD_SIDE_PIXELS,
            round(min(frame.width, frame.height) * MINIMUM_BOARD_SIDE_FRAME_FRACTION),
        )
        for group in row_groups:
            bounds = _robust_board_bounds(group)
            if bounds is None:
                continue
            left, top, right, bottom = bounds
            width = right - left
            height = bottom - top
            side = max(width, height)
            if min(width, height) < minimum_side or side == 0:
                continue
            shape_confidence = 1.0 - abs(width - height) / side
            if shape_confidence < MINIMUM_BOARD_SHAPE_CONFIDENCE:
                continue
            density = sum(
                min(row[3], width) for row in group if top <= row[0] < bottom
            ) / (width * height)
            if density < MINIMUM_BOARD_COLOR_DENSITY:
                continue
            region_confidence = min(
                1.0,
                shape_confidence * min(1.0, density / FULL_BOARD_COLOR_DENSITY),
            )
            region = BoardRegion(left, top, width, height, round(region_confidence, 6))
            candidates.append((width * height * density, region))
        if not candidates:
            return None
        candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        return candidates[0][1]

    # {
    #   責務: [_read_cell: cell四隅寄りの色sampleからtile数値または空cellを判定する]
    #   処理: [文字の重なりを避ける4つの小patchを多数決し、paletteにない画素を確信度へ反映しない]
    #   引数: [frame: 読取元BGRA画面, region: board矩形, row/column: 0始まりのtile位置]
    #   戻り値: [tuple[int | None, float]: tile値または未認識値と0から1の色一致率]
    # }
    def _read_cell(
        self,
        frame: ScreenFrame,
        region: BoardRegion,
        row: int,
        column: int,
    ) -> tuple[int | None, float]:
        center_x = BOARD_CENTER_FRACTIONS[column]
        center_y = BOARD_CENTER_FRACTIONS[row]
        samples: dict[int, int] = {}
        for vertical_offset in CELL_PATCH_OFFSETS:
            for horizontal_offset in CELL_PATCH_OFFSETS:
                x_fraction = center_x + (horizontal_offset - CELL_CENTER_FRACTION) * CELL_SIZE_FRACTION
                y_fraction = center_y + (vertical_offset - CELL_CENTER_FRACTION) * CELL_SIZE_FRACTION
                sample_x = region.x + round(region.width * x_fraction)
                sample_y = region.y + round(region.height * y_fraction)
                for y_delta in range(-CELL_PATCH_NEIGHBOR_RADIUS, CELL_PATCH_NEIGHBOR_RADIUS + 1):
                    for x_delta in range(-CELL_PATCH_NEIGHBOR_RADIUS, CELL_PATCH_NEIGHBOR_RADIUS + 1):
                        x = sample_x + x_delta
                        y = sample_y + y_delta
                        if not 0 <= x < frame.width or not 0 <= y < frame.height:
                            continue
                        pixel_offset = (y * frame.width + x) * FRAME_BYTES_PER_PIXEL
                        rgb = (
                            frame.bgra[pixel_offset + 2],
                            frame.bgra[pixel_offset + 1],
                            frame.bgra[pixel_offset],
                        )
                        value = CELL_VALUE_BY_RGB.get(rgb)
                        if value is None:
                            value = _nearest_cell_value(rgb)
                        if value is not None:
                            samples[value] = samples.get(value, 0) + 1
        if not samples:
            return None, 0.0
        ordered = sorted(samples.items(), key=lambda sample: sample[1], reverse=True)
        if len(ordered) > 1 and ordered[0][1] == ordered[1][1]:
            return None, 0.0
        valid_samples = sum(samples.values())
        confidence = ordered[0][1] / valid_samples
        if (
            confidence < MINIMUM_CELL_COLOR_CONFIDENCE
            or valid_samples < MINIMUM_VALID_CELL_COLOR_SAMPLES
        ):
            return None, round(confidence, 6)
        return ordered[0][0], round(confidence, 6)


def _nearest_cell_value(rgb: tuple[int, int, int]) -> int | None:
    # {
    #   責務: [_nearest_cell_value: RGB三成分の二乗距離が36以下のアンチエイリアス画素を既知cell色へ分類する]
    #   引数: [rgb: tile sample位置から読んだ1画素のRGB色]
    #   戻り値: [intまたはNone: 最も近い空cell/tile値。paletteから遠い文字色はNone]
    # }
    nearest_value = None
    nearest_distance = MAXIMUM_COLOR_DISTANCE_SQUARED + 1
    for palette_rgb, value in CELL_VALUE_BY_RGB.items():
        distance = sum((actual - expected) ** 2 for actual, expected in zip(rgb, palette_rgb))
        if distance < nearest_distance:
            nearest_value = value
            nearest_distance = distance
    return nearest_value if nearest_distance <= MAXIMUM_COLOR_DISTANCE_SQUARED else None
