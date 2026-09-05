import json
import unittest
from unittest.mock import patch

from ai_game_player.provider import OllamaProvider


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"models": [{"name": "gemma3:4b"}, {"name": "llama3:8b"}]}).encode("utf-8")


class ProviderModelsTest(unittest.TestCase):
    @patch("ai_game_player.provider.urlopen", return_value=FakeResponse())
    def test_lists_model_names(self, _urlopen):
        self.assertEqual(OllamaProvider.list_models(), ["gemma3:4b", "llama3:8b"])