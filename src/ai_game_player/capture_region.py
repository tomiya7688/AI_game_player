from dataclasses import dataclass
from enum import Enum
from math import ceil, floor, isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_game_player.capture_geometry import CaptureGeometry


# {
#   責務: [CONFIGURED_REGION_CONFIDENCE: 利用者が明示した矩形の解決成功を記録するconfidence値を定義する]
#   値: [1.0: 検出推定ではなく設定値から領域を決めたことを示す]
# }
CONFIGURED_REGION_CONFIDENCE = 1.0


# {
#   責務: [CaptureRegionMode: capture元の画面または対象windowから画素を選ぶ4方式を識別する]
#   値: [FULL_SCREEN: desktop全体, WINDOW: 選択window全体, FIXED_REGION: screen pixelで指定した固定領域, WINDOW_RELATIVE_REGION: 選択window内の正規化領域]
# }
class CaptureRegionMode(str, Enum):
    FULL_SCREEN = "full_screen"
    WINDOW = "window"
    FIXED_REGION = "fixed_region"
    WINDOW_RELATIVE_REGION = "window_relative_region"


# {
#   責務: [PixelRect: screen pixel座標またはframe pixel座標の矩形を左上と正の寸法で保持する]
#   フィールド: [x/y: pixel単位の左上座標, width/height: 正のpixel寸法]
# }
@dataclass(frozen=True)
class PixelRect:
    x: int
    y: int
    width: int
    height: int

    # {
    #   責務: [__post_init__: pixel矩形の座標と寸法を整数の有効値に限定する]
    #   戻り値: []
    #   エラー: [boolまたは整数以外の座標、非正寸法: 不正領域をcapture前に拒否]
    # }
    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ValueError("pixel rectangle coordinates and dimensions must be integers")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("pixel rectangle width and height must be greater than zero")

    # {
    #   責務: [right: pixel矩形の右端を排他的なscreen/frame座標で返す]
    #   戻り値: [int: x + width]
    # }
    @property
    def right(self) -> int:
        return self.x + self.width

    # {
    #   責務: [bottom: pixel矩形の下端を排他的なscreen/frame座標で返す]
    #   戻り値: [int: y + height]
    # }
    @property
    def bottom(self) -> int:
        return self.y + self.height

    # {
    #   責務: [contains: 引数矩形の全pixelがこの矩形に含まれるか判定する]
    #   引数: [other: 含有を確認するscreen/frame pixel矩形]
    #   戻り値: [bool: otherの全範囲を含む場合True]
    # }
    def contains(self, other: "PixelRect") -> bool:
        return self.x <= other.x and self.y <= other.y and self.right >= other.right and self.bottom >= other.bottom

    # {
    #   責務: [to_dict: pixel矩形を設定ファイルとObservationに保存できる辞書へ変換する]
    #   戻り値: [dict[str, int]: x/y/width/heightを持つJSON互換辞書]
    # }
    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


# {
#   責務: [NormalizedRect: window幅・高さに対する0から1までの相対矩形を保持する]
#   フィールド: [x/y: window寸法に対する左上比率, width/height: window寸法に対する正の比率]
# }
@dataclass(frozen=True)
class NormalizedRect:
    x: float
    y: float
    width: float
    height: float

    # {
    #   責務: [__post_init__: 正規化矩形がwindow内の有限な正領域になることを検証する]
    #   戻り値: []
    #   エラー: [NaN/無限値、bool、0未満または1超過の範囲: capture設定を拒否]
    # }
    def __post_init__(self) -> None:
        values = (self.x, self.y, self.width, self.height)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise ValueError("normalized rectangle values must be numbers")
        if any(not isfinite(value) for value in values):
            raise ValueError("normalized rectangle values must be finite")
        if self.width <= 0 or self.height <= 0 or self.x < 0 or self.y < 0:
            raise ValueError("normalized rectangle must have a positive size and non-negative origin")
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("normalized rectangle must stay within the window")

    # {
    #   責務: [to_dict: 正規化矩形をJSON互換辞書へ変換する]
    #   戻り値: [dict[str, float]: window比率のx/y/width/height]
    # }
    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


# {
#   責務: [CaptureRegion: captureモードと、そのモードに必要な固定または相対矩形を検証して保持する]
#   フィールド: [mode: 4種類のcaptureモード, rect: fixed pixel領域またはwindow相対領域。全体captureではNone]
# }
@dataclass(frozen=True)
class CaptureRegion:
    mode: CaptureRegionMode = CaptureRegionMode.WINDOW
    rect: PixelRect | NormalizedRect | None = None

    # {
    #   責務: [__post_init__: mode文字列を列挙型へ揃え、各modeと矩形型の組合せを検証する]
    #   戻り値: []
    #   エラー: [未対応mode、modeと矩形の型・有無が不一致: 不正な永続設定を拒否]
    # }
    def __post_init__(self) -> None:
        try:
            mode = CaptureRegionMode(self.mode)
        except ValueError as error:
            raise ValueError(f"unsupported capture mode: {self.mode}") from error
        object.__setattr__(self, "mode", mode)
        if mode is CaptureRegionMode.FIXED_REGION and not isinstance(self.rect, PixelRect):
            raise ValueError("fixed_region requires a pixel rectangle")
        if mode is CaptureRegionMode.WINDOW_RELATIVE_REGION and not isinstance(self.rect, NormalizedRect):
            raise ValueError("window_relative_region requires a normalized rectangle")
        if mode in {CaptureRegionMode.FULL_SCREEN, CaptureRegionMode.WINDOW} and self.rect is not None:
            raise ValueError(f"{mode.value} does not accept a rectangle")

    # {
    #   責務: [to_dict: capture modeと領域を設定ファイルへ保存できる辞書に変換する]
    #   戻り値: [dict[str, object]: modeと必要な場合のrectを持つJSON互換辞書]
    # }
    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"mode": self.mode.value}
        if self.rect is not None:
            value["rect"] = self.rect.to_dict()
        return value

    # {
    #   責務: [from_dict: config.jsonのcapture_region値を検証済みCaptureRegionへ復元する]
    #   引数: [value: modeと任意rectを含むJSON object]
    #   戻り値: [CaptureRegion: 不正なmodeと領域の組合せを含まない設定]
    #   エラー: [値がobjectでない、必須キーや数値が欠ける、領域が範囲外: 設定読込を失敗させる]
    # }
    @classmethod
    def from_dict(cls, value: object) -> "CaptureRegion":
        if not isinstance(value, dict):
            raise ValueError("capture_region must be an object")
        try:
            mode = CaptureRegionMode(value["mode"])
        except (KeyError, ValueError, TypeError) as error:
            raise ValueError("capture_region.mode must name a supported capture mode") from error
        raw_rect = value.get("rect")
        rect: PixelRect | NormalizedRect | None = None
        if raw_rect is not None:
            if not isinstance(raw_rect, dict):
                raise ValueError("capture_region.rect must be an object")
            try:
                raw_values = (raw_rect["x"], raw_rect["y"], raw_rect["width"], raw_rect["height"])
                if mode is CaptureRegionMode.FIXED_REGION:
                    if any(isinstance(item, bool) or not isinstance(item, int) for item in raw_values):
                        raise ValueError("fixed region values must be integers")
                    rect = PixelRect(*raw_values)
                else:
                    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in raw_values):
                        raise ValueError("normalized region values must be numbers")
                    rect = NormalizedRect(*(float(item) if not isinstance(item, bool) else item for item in raw_values))
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                raise ValueError(f"capture_region.rect is invalid: {error}") from error
        return cls(mode, rect)

    # {
    #   責務: [capture_handle: modeに応じてdesktopまたは選択windowをcaptureするHWNDを返す]
    #   引数: [window_handle: 利用者が選んだ対象window。Noneは選択なし]
    #   戻り値: [int | None: desktop captureならNone、window captureなら選択HWND]
    #   エラー: [window modeまたは相対ROIでwindow未選択: 不明な座標系でのcaptureを防ぐ]
    # }
    def capture_handle(self, window_handle: int | None) -> int | None:
        if self.mode in {CaptureRegionMode.FULL_SCREEN, CaptureRegionMode.FIXED_REGION}:
            return None
        if window_handle is None:
            raise ValueError(
                f"取得範囲「{self.mode.value}」には対象ウィンドウが必要です。"
                "画面上で操作対象ウィンドウを選択してください。"
            )
        return window_handle

    # {
    #   責務: [resolve: frame全体のscreen mappingと選択windowからcrop pixel範囲と変換情報を解決する]
    #   引数: [frame_width/frame_height: crop前BGRA frame寸法, screen_bounds: frame全体に対応するscreen矩形, window_bounds: 選択HWNDのscreen矩形またはNone]
    #   戻り値: [CaptureGeometry: crop範囲、座標変換、領域出所、確信度を保持するgeometry]
    #   エラー: [fixed ROIが取得desktop外、またはwindow modeに対象window boundsがない: 切り詰めずcaptureを拒否]
    # }
    def resolve(
        self,
        frame_width: int,
        frame_height: int,
        screen_bounds: PixelRect,
        window_bounds: PixelRect | None,
    ) -> "CaptureGeometry":
        from ai_game_player.capture_geometry import CaptureGeometry

        source_frame = PixelRect(0, 0, frame_width, frame_height)
        if self.mode is CaptureRegionMode.FULL_SCREEN:
            crop = source_frame
        elif self.mode is CaptureRegionMode.WINDOW:
            if window_bounds is None:
                raise ValueError("選択ウィンドウの位置を取得できません。ウィンドウ一覧を更新して選び直してください。")
            crop = source_frame
        elif self.mode is CaptureRegionMode.FIXED_REGION:
            assert isinstance(self.rect, PixelRect)
            if not screen_bounds.contains(self.rect):
                raise ValueError(
                    "固定ROIの一部が主画面外です。主画面内に収まるX・Y・幅・高さを指定してください。"
                )
            left = floor((self.rect.x - screen_bounds.x) * frame_width / screen_bounds.width)
            top = floor((self.rect.y - screen_bounds.y) * frame_height / screen_bounds.height)
            right = ceil((self.rect.right - screen_bounds.x) * frame_width / screen_bounds.width)
            bottom = ceil((self.rect.bottom - screen_bounds.y) * frame_height / screen_bounds.height)
            crop = PixelRect(left, top, right - left, bottom - top)
        else:
            if window_bounds is None:
                raise ValueError(
                    "ウィンドウ相対ROIを解決できません。対象ウィンドウを選び直してください。"
                )
            assert isinstance(self.rect, NormalizedRect)
            left = floor(self.rect.x * frame_width)
            top = floor(self.rect.y * frame_height)
            right = ceil((self.rect.x + self.rect.width) * frame_width)
            bottom = ceil((self.rect.y + self.rect.height) * frame_height)
            crop = PixelRect(left, top, right - left, bottom - top)
        return CaptureGeometry(
            self.mode,
            crop,
            frame_width,
            frame_height,
            screen_bounds,
            window_bounds,
            CONFIGURED_REGION_CONFIDENCE,
            f"configured:{self.mode.value}",
        )
