"""保存済み2048失敗bundleの画素を検証し、OCRなしの盤面認識を再実行する。"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from ai_game_player.capture_geometry import BGRA_BYTES_PER_PIXEL
from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame


MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 64 * 1024 * 1024
FRAME_FILES = {"last_playable": "last-playable.bgra", "last_capture": "last-captured.bgra"}


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"manifestにJSON標準外の数値{value}があります。元artifactを確認してください")


class RecognitionEvidenceReplay:
    """manifestの基準directoryと現行Adapterを保持し、記録済み盤面を画素から再検査する。"""

    def __init__(self, manifest_path: Path) -> None:
        self.manifest_path = manifest_path.resolve(strict=True)
        self.directory = self.manifest_path.parent
        self.adapter = ReferenceGame2048Adapter()

    def run(self) -> dict[str, object]:
        """画像付きv1 bundleを検証し、各frameのhash・認識結果・比較結果を返す。元の資料は変更しない。"""
        with self.manifest_path.open("rb") as stream:
            encoded = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError("manifestが1 MiBを超えています。元artifactのmanifestを確認してください")
        payload = json.loads(encoded, parse_constant=_reject_nonfinite_json)
        if not isinstance(payload, dict) or payload.get("schema") != "2048-recognition-evidence/v1":
            raise ValueError("manifestのschemaが2048-recognition-evidence/v1ではありません")
        frames = payload.get("frames")
        if not isinstance(frames, dict) or not frames or frames.keys() - FRAME_FILES.keys():
            raise ValueError("manifestのframesにはlast_playableまたはlast_captureが必要です")
        results = {role: self._replay_frame(role, metadata) for role, metadata in frames.items()}
        return {"schema": "2048-recognition-replay/v1", "ocr_enabled": False, "frames": results}

    def _replay_frame(self, role: str, metadata: object) -> dict[str, object]:
        if not isinstance(metadata, dict):
            raise ValueError(f"{role}: frame情報はJSON objectで指定してください")
        frame = self._read_frame(role, metadata)
        adapted, candidates = self.adapter.adapt(
            frame, ScreenObservation("2048-evidence-replay", frame.width, frame.height)
        )
        current = adapted.features["game_2048"]
        recorded = metadata.get("game_state")
        if not isinstance(recorded, dict):
            raise ValueError(f"{role}: game_stateがJSON objectではありません")
        # score・state_signature・OCR由来の終了判定は画素だけでは同じ入力を再現できない。
        comparison = {
            key: recorded[key] == current[key] if key in recorded else None
            for key in ("board", "confidence")
        }
        return {
            "bgra_sha256": sha256(frame.bgra).hexdigest(),
            "recorded_game_state": recorded,
            "replayed_game_state": current,
            "visual_matches": comparison,
            "candidate_count": len(candidates),
        }

    def _read_frame(self, role: str, metadata: dict) -> ScreenFrame:
        """bundle直下の固定名BGRAを64 MiB以内で読み、寸法とSHA-256が一致した画素だけを返す。"""
        width, height = metadata.get("width"), metadata.get("height")
        if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
            raise ValueError(f"{role}: widthとheightには正の整数が必要です")
        expected_size = width * height * BGRA_BYTES_PER_PIXEL
        if expected_size > MAX_FRAME_BYTES:
            raise ValueError(f"{role}: BGRAが64 MiBを超えます。寸法と元artifactを確認してください")
        expected_name = FRAME_FILES[role]
        if metadata.get("bgra") != expected_name:
            raise ValueError(f"{role}: bgraファイル名は{expected_name}である必要があります")
        path = (self.directory / expected_name).resolve(strict=True)
        if path.parent != self.directory:
            raise ValueError(f"{role}: bundle外のBGRAファイルは読み込みません")
        with path.open("rb") as stream:
            pixels = stream.read(expected_size + 1)
        if len(pixels) != expected_size:
            raise ValueError(f"{role}: BGRA byte数がwidth×height×4と一致しません")
        if sha256(pixels).hexdigest() != metadata.get("bgra_sha256"):
            raise ValueError(f"{role}: BGRAのSHA-256がmanifestと一致しません。元artifactを再取得してください")
        return ScreenFrame(width, height, pixels)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="展開済み失敗bundleのmanifest.json")
    arguments = parser.parse_args()
    try:
        report = RecognitionEvidenceReplay(arguments.manifest).run()
    except (OSError, ValueError, RecursionError) as error:
        parser.error(f"2048の保存画面を再解析できません: {error}")
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    # Noneは解析例外などで比較元がなかった項目。比較済みの不一致だけを終了値1にする。
    return int(any(
        matches is False
        for result in report["frames"].values()
        for matches in result["visual_matches"].values()
    ))


if __name__ == "__main__":
    raise SystemExit(main())
