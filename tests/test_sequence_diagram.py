import tempfile
import unittest
from pathlib import Path

from tools.generate_sequence_diagram import render_sequence_diagram


class SequenceDiagramTest(unittest.TestCase):
    def test_renders_direct_method_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.py"
            source.write_text("class Runner:\n    def run(self):\n        self.source.read()\n        self.engine.step()\n", encoding="utf-8")
            diagram = render_sequence_diagram(source, "Runner", "run")
            self.assertIn("sequenceDiagram", diagram)
            self.assertIn("Runner.source.read()", diagram)
            self.assertIn("Runner.engine.step()", diagram)