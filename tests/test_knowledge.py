import tempfile
import unittest
from pathlib import Path
from ai_game_player.knowledge import KnowledgeStore
class KnowledgeTest(unittest.TestCase):
    def test_add_and_search(self):
        with tempfile.TemporaryDirectory() as d:
            store=KnowledgeStore(Path(d)/"knowledge.json"); store.add("button","shop","opens the shop")
            self.assertEqual(store.search("SHOP")[0]["category"],"button")
            self.assertEqual(store.search("shop", "enemy"),[])