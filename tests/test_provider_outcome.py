import json
import unittest
from unittest.mock import patch

from ai_game_player.models import ScreenObservation
from ai_game_player.provider import OllamaProvider


class FakeResponse:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps({"response": json.dumps({"status": "success", "confidence": 0.8, "reason": "clear screen"})}).encode("utf-8")


class ProviderOutcomeTest(unittest.TestCase):
    @patch("ai_game_player.provider.urlopen", return_value=FakeResponse())
    def test_assesses_outcome(self, _urlopen):
        result = OllamaProvider("model").assess_outcome(ScreenObservation("s", 10, 10))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.confidence, 0.8)