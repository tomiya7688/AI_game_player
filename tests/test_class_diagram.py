import tempfile
import unittest
from pathlib import Path

from tools.generate_class_diagram import render_class_diagram


class ClassDiagramTest(unittest.TestCase):
    def test_renders_classes_bases_and_methods(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_text("class Child(Base):\n    def run(self):\n        pass\n", encoding="utf-8")
            diagram = render_class_diagram(root)
            self.assertIn("classDiagram", diagram)
            self.assertIn("Base <|-- Child", diagram)
            self.assertIn("+run()", diagram)