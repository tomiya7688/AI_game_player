import tempfile
import unittest
from pathlib import Path
from ai_game_player.config import AppConfig, ConfigStore
class ConfigTest(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            store=ConfigStore(Path(d)/"config.json"); expected=AppConfig("Ollama","qwen","http://x","慎重","探索"); store.save(expected); self.assertEqual(store.load(),expected)
    def test_missing_config_uses_defaults(self):
        with tempfile.TemporaryDirectory() as d: self.assertEqual(ConfigStore(Path(d)/"none.json").load(),AppConfig())