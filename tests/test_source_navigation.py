"""Keep the two source-navigation pages linked to existing repository paths."""

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


class SourceNavigationTests(unittest.TestCase):
    def test_relative_markdown_links_point_to_existing_paths(self):
        repository_root = Path(__file__).resolve().parents[1]
        for relative_page in ("README.md", "src/ai_game_player/README.md"):
            page = repository_root / relative_page
            links = re.findall(
                r"\[[^\]]+\]\(([^)]+)\)", page.read_text(encoding="utf-8")
            )
            self.assertTrue(links, f"No navigation links in {relative_page}")
            for link in links:
                url = urlsplit(link)
                if url.scheme or url.netloc or not url.path:
                    continue
                with self.subTest(page=relative_page, link=link):
                    target = (page.parent / unquote(url.path)).resolve()
                    self.assertTrue(target.is_relative_to(repository_root))
                    self.assertTrue(target.exists(), f"Broken relative link: {link}")
