"""実ブラウザで生成した終了画面を再生する。実OSのHWND取得や入力は検証しない。"""

from hashlib import sha256
import json
from pathlib import Path
import unittest
import zlib

from ai_game_player.game_2048_adapter import ReferenceGame2048Adapter
from ai_game_player.models import ScreenObservation
from ai_game_player.screen_capture import ScreenFrame
from tools.reference_game_2048_source import SOURCE_LOCK_PATH


FIXTURES = Path(__file__).parent / "fixtures/2048/terminal-render"


class Rendered2048TerminalTests(unittest.TestCase):
    def test_terminal_text_and_retry_button_preserve_board_recognition_without_ocr(self):
        source_commit = json.loads(SOURCE_LOCK_PATH.read_text(encoding="utf-8"))["commit"]
        for name in ("terminal-0.75", "terminal-1"):
            with self.subTest(fixture=name):
                metadata = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual(metadata["source_commit"], source_commit, "固定版変更時は描画fixtureを再生成してください")
                pixels = zlib.decompress((FIXTURES / f"{name}.bgra.zlib").read_bytes())
                self.assertEqual(sha256(pixels).hexdigest(), metadata["bgra_sha256"])
                frame = ScreenFrame(metadata["width"], metadata["height"], pixels)
                adapted, candidates = ReferenceGame2048Adapter().adapt(
                    frame, ScreenObservation("rendered-terminal-replay", frame.width, frame.height)
                )
                state = adapted.features["game_2048"]
                self.assertEqual(state["board"], metadata["expected_board"])
                self.assertIs(state["game_over"], True)
                self.assertGreaterEqual(state["confidence"], 0.65)
                self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
