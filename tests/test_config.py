import json
import tempfile
import unittest
from pathlib import Path

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