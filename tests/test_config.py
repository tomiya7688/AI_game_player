import json
import tempfile
import unittest
from pathlib import Path

from ai_game_player.capture_region import CaptureRegion, CaptureRegionMode, NormalizedRect, PixelRect
from ai_game_player.config import AppConfig, ConfigStore


class ConfigTest(unittest.TestCase):
    def test_missing_config_uses_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(ConfigStore(Path(directory) / "missing.json").load(), AppConfig())

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = AppConfig(provider="Ollama", model="m", endpoint="http://x", personality="p", purpose="q", live_execution=True, input_mode="mouse")
            store = ConfigStore(path)
            store.save(config)
            self.assertEqual(store.load(), config)

    def test_string_false_does_not_enable_live_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"live_execution": "false"}), encoding="utf-8")
            self.assertFalse(ConfigStore(path).load().live_execution)

    def test_capture_region_mode_and_rectangle_survive_config_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = AppConfig(
                capture_region=CaptureRegion(
                    CaptureRegionMode.WINDOW_RELATIVE_REGION,
                    NormalizedRect(0.1, 0.2, 0.5, 0.6),
                )
            )

            ConfigStore(path).save(config)

            self.assertEqual(ConfigStore(path).load(), config)

    def test_old_config_defaults_to_window_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"model": "legacy"}), encoding="utf-8")

            config = ConfigStore(path).load()

            self.assertEqual(config.model, "legacy")
            self.assertEqual(config.capture_region, CaptureRegion())

    def test_invalid_persisted_capture_region_fails_with_field_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "capture_region": {
                            "mode": CaptureRegionMode.FIXED_REGION.value,
                            "rect": PixelRect(0, 0, 10, 10).to_dict(),
                        }
                    }
                ),
                encoding="utf-8",
            )
            value = json.loads(path.read_text(encoding="utf-8"))
            value["capture_region"]["rect"]["width"] = 0
            path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "config.json capture_region is invalid"):
                ConfigStore(path).load()
