from pathlib import Path
import tempfile
import unittest
from urllib.request import urlopen

from tools.reference_game_seed import seeded_index
from tools.reference_game_2048_launcher import LoopbackStaticServer


class ReferenceGameSeedTests(unittest.TestCase):
    def test_explicit_seed_only_changes_served_response_not_pinned_files(self):
        original = (
            b'<html><head></head><body><script src="game.js"></script></body></html>'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_bytes(original)
            self.assertEqual(seeded_index(root, None), (None, None))
            first, metadata = seeded_index(root, 479)
            self.assertEqual(seeded_index(root, 479), (first, metadata))
            other, other_metadata = seeded_index(root, 480)
            self.assertNotEqual(first, other)
            self.assertNotEqual(
                metadata["initialization_script_sha256"],
                other_metadata["initialization_script_sha256"],
            )
            server = LoopbackStaticServer()
            server.test_seed = 479
            try:
                url = server.start(root)
                with urlopen(url, timeout=2) as response:
                    self.assertEqual(response.read(), first)
                self.assertEqual(server.test_seed_metadata, metadata)
            finally:
                server.close()
            self.assertEqual((root / "index.html").read_bytes(), original)

    def test_invalid_seed_and_unknown_or_oversized_html_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_bytes(b"<head></head>")
            for seed in (True, -1, 2**32, "479"):
                with self.assertRaises(ValueError):
                    seeded_index(root, seed)
            for body in (b"<html/>", b"<head>" + b"x" * 262144):
                (root / "index.html").write_bytes(body)
                with self.assertRaises(ValueError):
                    seeded_index(root, 479)
