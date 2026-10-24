"""Opt-in evidence for recognition failures in the owned, pinned 2048 window."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import struct
import tempfile
import zlib

from ai_game_player.capture_geometry import BGRA_BYTES_PER_PIXEL
from ai_game_player.screen_capture import ScreenFrame


RGB_BYTES_PER_PIXEL = 3
PNG_BIT_DEPTH = 8
PNG_COLOR_TYPE_RGB = 2


# {
#   責務: [RecognitionFailureEvidence: 固定版2048の認識失敗を、最後に受理した盤面と最新の取得画面を比較して調査できる形で保存する]
#   保持状態: [output_directory: episode専用の出力先, last_playable: 最後に受理したframe参照と認識結果。通常の観測中はファイルを作成しない]
# }
class RecognitionFailureEvidence:
    # {
    #   責務: [__init__: episodeの診断保存先を記憶し、最後に受理した画面を未取得状態にする]
    #   引数: [output_directory: 失敗bundleを追加するepisode専用directory。初期化時には作成しない]
    #   戻り値: [なし]
    # }
    def __init__(self, output_directory: Path) -> None:
        self.output_directory = output_directory
        self._last_playable: tuple[ScreenFrame, dict[str, object]] | None = None

    # {
    #   責務: [record_playable: 失敗画面との比較に使う、最後に受理した盤面をメモリに保持する]
    #   引数: [frame: 受理した盤面を認識した同一のBGRA画面, game_state: boardとconfidenceを含むAdapterの認識結果]
    #   戻り値: [なし。画素bytesは複製せず参照を保持し、認識結果は後続の変更から分離する]
    # }
    def record_playable(self, frame: ScreenFrame, game_state: dict[str, object]) -> None:
        self._last_playable = (frame, deepcopy(game_state))

    # {
    #   責務: [save_failure: 失敗時だけ新規directoryへ画面と診断値を保存し、manifestの場所を返す]
    #   引数: [frame: 最後に取得した画面。取得前の失敗ならNone, game_state: 最新画面の認識結果, diagnostics: 元の失敗理由・対象HWND・試行数など]
    #   戻り値: [Path: 保存を完了したmanifest.json。画像は表示用PNGと完全再現用BGRAを対にして保存する]
    #   エラー: [OSErrorまたはValueError: 保存失敗。呼び出し側は元の認識失敗を維持し、診断保存の失敗を併記する]
    # }
    def save_failure(
        self,
        frame: ScreenFrame | None,
        game_state: dict[str, object],
        diagnostics: dict[str, object],
    ) -> Path:
        self.output_directory.mkdir(parents=True, exist_ok=True)
        bundle_directory = Path(tempfile.mkdtemp(prefix="recognition-failure-", dir=self.output_directory))
        frames: dict[str, object] = {}
        if self._last_playable is not None:
            accepted_frame, accepted_state = self._last_playable
            frames["last_playable"] = _write_frame(bundle_directory, "last-playable", accepted_frame, accepted_state)
        if frame is not None:
            frames["last_capture"] = _write_frame(bundle_directory, "last-captured", frame, game_state)
        manifest_path = bundle_directory / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {"schema": "2048-recognition-evidence/v1", "diagnostics": diagnostics, "frames": frames},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            ) + "\n",
            encoding="utf-8",
        )
        return manifest_path


# {
#   責務: [_write_frame: 一つの取得画面をPNGとpacked BGRAで保存し、寸法・hash・認識結果を対応付ける]
#   引数: [directory: 新規bundle directory, name: writer内部で決めたファイル名, frame: 保存する画面, game_state: 同じ画面の認識結果]
#   戻り値: [dict: 保存ファイル名、BGRAのSHA-256、frame取得情報、board認識結果を持つmanifest項目]
# }
def _write_frame(directory: Path, name: str, frame: ScreenFrame, game_state: dict[str, object]) -> dict[str, object]:
    png_bytes = _encode_png(frame)
    png_name = f"{name}.png"
    bgra_name = f"{name}.bgra"
    (directory / png_name).write_bytes(png_bytes)
    (directory / bgra_name).write_bytes(frame.bgra)
    return {
        "png": png_name,
        "bgra": bgra_name,
        "bgra_sha256": sha256(frame.bgra).hexdigest(),
        "width": frame.width,
        "height": frame.height,
        "captured_at": frame.captured_at,
        "frame_id": frame.frame_id,
        "screen_bounds": asdict(frame.screen_bounds) if frame.screen_bounds is not None else None,
        "window_bounds": asdict(frame.window_bounds) if frame.window_bounds is not None else None,
        "game_state": game_state,
    }


# {
#   責務: [_encode_png: BGRA画面を外部画像ライブラリなしでRGB PNGへ変換する]
#   引数: [frame: 上から下へのrow順でpaddingのないBGRA画素を持つ取得画面]
#   戻り値: [bytes: RGBを無損失で保持するPNG。alphaは表示に使わず、元のalphaは対のBGRAファイルへ保存する]
#   エラー: [ValueError: 画面寸法またはBGRA byte数が不正]
# }
def _encode_png(frame: ScreenFrame) -> bytes:
    if frame.width <= 0 or frame.height <= 0 or len(frame.bgra) != frame.width * frame.height * BGRA_BYTES_PER_PIXEL:
        raise ValueError("Evidence frame dimensions do not match its packed BGRA bytes")
    scanlines = bytearray()
    for y in range(frame.height):
        start = y * frame.width * BGRA_BYTES_PER_PIXEL
        end = start + frame.width * BGRA_BYTES_PER_PIXEL
        row = bytearray(1 + frame.width * RGB_BYTES_PER_PIXEL)
        row[1::RGB_BYTES_PER_PIXEL] = frame.bgra[start + 2:end:BGRA_BYTES_PER_PIXEL]
        row[2::RGB_BYTES_PER_PIXEL] = frame.bgra[start + 1:end:BGRA_BYTES_PER_PIXEL]
        row[3::RGB_BYTES_PER_PIXEL] = frame.bgra[start:end:BGRA_BYTES_PER_PIXEL]
        scanlines.extend(row)
    header = struct.pack(">IIBBBBB", frame.width, frame.height, PNG_BIT_DEPTH, PNG_COLOR_TYPE_RGB, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


# {
#   責務: [_png_chunk: PNGのchunk種別と内容に、4 byteの長さとCRC-32を付ける]
#   引数: [kind: IHDRなど4 byteのchunk種別, data: chunkへ格納するbinary値]
#   戻り値: [bytes: PNG仕様のbig-endian長さ・種別・内容・CRCを連結した一つのchunk]
# }
def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
