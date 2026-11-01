"""明示比較の失敗時だけ、所有ゲームの既存header画素を有界にログへ輸送する。"""

import base64
from hashlib import sha256
from io import BytesIO
import json
from math import isfinite
from typing import Any, Callable, Mapping

from ai_game_player.screen_capture import ScreenFrame
from ai_game_player.safety_guard import TargetState

MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_PNG_BYTES = 128 * 1024
MAX_HEADER_WIDTH = 640
MAX_HEADER_HEIGHT = 240
CHUNK_CHARACTERS = 4096


# {
#   責務: [OwnedGameFailureHeader: 一episode一回の診断画像だけをstdoutへ送る]
#   フィールド: [enabled: 既存save-failure-evidenceの明示指定, provider/seed: 比較試行,
#     _attempted: 二重送信防止, _write_line/_probe: stdoutとread-only所有照合の差替点]
#   処理: [Sourceの再capture、cache、lease、学習、入力許可には一切触れない]
# }
class OwnedGameFailureHeader:
    # {
    #   責務: [__init__: 比較試行の明示条件と診断の出力先を保持する]
    #   引数: [enabled: save-failure-evidence, provider: 比較の三判断元, seed: uint32,
    #     write_line: JSON一行出力, probe: 任意の既存TargetProbe.inspect相当]
    #   戻り値: [なし] エラー: [ValueError: 明示条件違反]
    # }
    def __init__(
        self,
        *,
        enabled: bool,
        provider: str,
        seed: int,
        write_line: Callable[[str], Any] = print,
        probe: Callable[[int], TargetState] | None = None,
    ) -> None:
        if type(enabled) is not bool or provider not in {
            "cycling",
            "baseline",
            "planner",
        }:
            raise ValueError("failure header requires an explicit comparison profile")
        if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
            raise ValueError("failure header comparison seed must be uint32")
        self.enabled, self.provider, self.seed = enabled, provider, seed
        self._write_line, self._probe = write_line, probe
        self._attempted = False

    # {
    #   責務: [emit: 既存の失敗frameのheaderを一回だけchunk輸送する]
    #   引数: [episode: 専用ReferenceGame2048Episode, failed: 確定した比較失敗,
    #     frame: 失敗判定と同じ既存BGRA, crop_bbox: frame座標のheader矩形,
    #     capture_diagnostics: 同frameのbackend/生産/host受領時刻]
    #   戻り値: [dict: 画像を含まないready/unavailable結果。権限として使わない]
    #   エラー: [診断失敗はunavailableへ限定し、元の比較失敗を上書きしない]
    # }
    def emit(
        self,
        episode: Any,
        *,
        failed: bool,
        frame: ScreenFrame | None,
        crop_bbox: Any,
        capture_diagnostics: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled or failed is not True:
            return {"status": "disabled" if not self.enabled else "not_failure"}
        if self._attempted:
            return {"status": "already_attempted"}
        self._attempted = True
        try:
            handle, pid = self._owned_target(episode)
            self._validate_frame(frame)
            assert frame is not None
            crop = self._validate_crop(frame, crop_bbox)
            capture = self._capture_reference(frame, capture_diagnostics)
            png = self._encode_png(frame, crop)
            if not 0 < len(png) <= MAX_PNG_BYTES:
                raise ValueError("png_size_limit")
            frame_digest, png_digest = (
                sha256(frame.bgra).hexdigest(),
                sha256(png).hexdigest(),
            )
            encoded = base64.b64encode(png).decode("ascii")
            chunks = [
                encoded[i : i + CHUNK_CHARACTERS]
                for i in range(0, len(encoded), CHUNK_CHARACTERS)
            ]
            image_id = f"{self.provider}.{self.seed}.{frame_digest}"
            metadata = {
                "event": "windows_planning_failure_header",
                "schema_version": 1,
                "status": "ready",
                "scope": "owned_reference_game_2048_failure_header_only",
                "diagnostic_only": True,
                "provider": self.provider,
                "seed": self.seed,
                "image_id": image_id,
                "frame_sha256": frame_digest,
                "png_sha256": png_digest,
                "png_bytes": len(png),
                "chunk_count": len(chunks),
                "original_frame_size": [frame.width, frame.height],
                "crop_bbox": list(crop),
                "png_size": [crop[2], crop[3]],
                "selected_hwnd": handle,
                "owned_pid": pid,
                "capture": capture,
            }
        except Exception as error:
            reason = (
                str(error)
                if type(error) is ValueError
                else "png_encoder_unavailable"
                if isinstance(error, ImportError)
                else "diagnostic_unavailable"
            )
            if reason not in {
                "ownership_unverified",
                "invalid_frame",
                "invalid_crop",
                "invalid_capture_reference",
                "png_size_limit",
                "png_encoder_unavailable",
            }:
                reason = "diagnostic_unavailable"
            result = {
                "event": "windows_planning_failure_header",
                "schema_version": 1,
                "status": "unavailable",
                "provider": self.provider,
                "seed": self.seed,
                "reason": reason,
            }
            self._write_safely(result)
            return result
        if not self._write_safely(metadata):
            return {"status": "unavailable", "reason": "stdout_unavailable"}
        for index, chunk in enumerate(chunks):
            if not self._write_safely(
                {
                    "event": "windows_planning_failure_header_chunk",
                    "image_id": image_id,
                    "index": index,
                    "count": len(chunks),
                    "png_sha256": png_digest,
                    "png_bytes": len(png),
                    "base64": chunk,
                }
            ):
                return {"status": "unavailable", "reason": "stdout_unavailable"}
        return metadata

    # {
    #   責務: [_owned_target: 専用launcherの生存PID/HWNDを既存read-only probeで再照合する]
    #   引数: [episode: 比較対象] 戻り値: [tuple: 検証済みHWND/PID]
    #   エラー: [ValueError: 製品/desktop/所有不明の対象を拒否]
    # }
    def _owned_target(self, episode: Any) -> tuple[int, int]:
        from ai_game_player.safety_guard import WindowsTargetProbe
        from tools.run_2048_campaign import ReferenceGame2048Episode

        if type(episode) is not ReferenceGame2048Episode:
            raise ValueError("ownership_unverified")
        info, process = episode.launcher.window_info, episode.launcher.browser_process
        source = episode.source
        if info is None or process is None or source is None:
            raise ValueError("ownership_unverified")
        handle, pid = info.handle, process.pid
        if (
            type(handle) is not int
            or not 0 < handle <= 0xFFFFFFFFFFFFFFFF
            or type(pid) is not int
            or not 0 < pid <= 0xFFFFFFFF
            or info.process_id != pid
            or source.window_handle != handle
            or process.poll() is not None
        ):
            raise ValueError("ownership_unverified")
        target = (self._probe or WindowsTargetProbe().inspect)(handle)
        if target.valid is not True or target.handle != handle or target.pid != pid:
            raise ValueError("ownership_unverified")
        return handle, pid

    # {
    #   責務: [_validate_frame: 保持済みBGRA以外を読まず、入力byte/寸法/時刻を有界にする]
    #   引数: [frame: 診断元] 戻り値: [なし] エラー: [ValueError: frame不正]
    # }
    @staticmethod
    def _validate_frame(frame: ScreenFrame | None) -> None:
        if (
            not isinstance(frame, ScreenFrame)
            or type(frame.width) is not int
            or frame.width <= 0
            or type(frame.height) is not int
            or frame.height <= 0
            or type(frame.bgra) is not bytes
            or not 0 < len(frame.bgra) <= MAX_FRAME_BYTES
            or len(frame.bgra) != frame.width * frame.height * 4
            or type(frame.captured_at) not in (int, float)
            or not isfinite(frame.captured_at)
            or frame.captured_at <= 0
        ):
            raise ValueError("invalid_frame")

    # {
    #   責務: [_validate_crop: 呼出元の既存header矩形を拡張せず検証する]
    #   引数: [frame: 元寸法, box: x/y/width/height] 戻り値: [tuple: 同じ矩形]
    #   エラー: [ValueError: 範囲外・上限超過]
    # }
    @staticmethod
    def _validate_crop(frame: ScreenFrame, box: Any) -> tuple[int, int, int, int]:
        if (
            type(box) not in (tuple, list)
            or len(box) != 4
            or any(type(v) is not int for v in box)
        ):
            raise ValueError("invalid_crop")
        x, y, width, height = box
        if not (
            0 <= x
            and 0 <= y
            and 0 < width <= MAX_HEADER_WIDTH
            and 0 < height <= MAX_HEADER_HEIGHT
            and x + width <= frame.width
            and y + height <= frame.height
        ):
            raise ValueError("invalid_crop")
        return x, y, width, height

    # {
    #   責務: [_capture_reference: 診断時刻を元frameへ束縛し、入力許可や鮮度判定に転用しない]
    #   引数: [frame: 元時刻, values: 同frameの取得診断] 戻り値: [dict: 固定項目のみ]
    #   エラー: [ValueError: 不正/別frame参照]
    # }
    @staticmethod
    def _capture_reference(
        frame: ScreenFrame, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        names = ("request_started_ns", "produced_at_ns", "host_received_ns")
        if (
            values.get("backend") not in {"wgc", "gdi"}
            or any(
                type(values.get(k)) is not int
                or not 0 < values[k] <= 0xFFFFFFFFFFFFFFFF
                for k in names
            )
            or values["produced_at_ns"] != int(frame.captured_at * 1_000_000_000)
        ):
            raise ValueError("invalid_capture_reference")
        return {"backend": values["backend"], **{k: values[k] for k in names}}

    # {
    #   責務: [_encode_png: 検証済みheader行だけをコピーしRGB PNGへ変換する]
    #   引数: [frame: 既存BGRA, crop: 検証済み矩形] 戻り値: [bytes: PNG]
    #   エラー: [ImportError/ValueError: 診断不可として呼出元で保持]
    # }
    @staticmethod
    def _encode_png(frame: ScreenFrame, crop: tuple[int, int, int, int]) -> bytes:
        from PIL import Image

        x, y, width, height = crop
        view = memoryview(frame.bgra)
        pixels = b"".join(
            view[
                ((y + row) * frame.width + x) * 4 : (
                    (y + row) * frame.width + x + width
                )
                * 4
            ]
            for row in range(height)
        )
        image = Image.frombytes("RGB", (width, height), pixels, "raw", "BGRX")
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    # {
    #   責務: [_write_safely: 画像行の出力失敗で元の試行失敗を覆わない]
    #   引数: [value: 有界JSON] 戻り値: [bool: 行出力完了]
    # }
    def _write_safely(self, value: dict[str, Any]) -> bool:
        try:
            self._write_line(
                json.dumps(
                    value, ensure_ascii=True, separators=(",", ":"), allow_nan=False
                )
            )
        except Exception:
            return False
        return True
