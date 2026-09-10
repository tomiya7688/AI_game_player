import json
import tempfile
import unittest
from pathlib import Path

from tools.generate_docs import generated_documents, stale_documents, write_documents


class GenerateDocsTest(unittest.TestCase):
    def test_generates_and_detects_stale_diagrams(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "ai_game_player"
            source.mkdir(parents=True)
            (source / "pipeline.py").write_text(
                "class DecisionPipeline:\n    def run_and_execute(self):\n        self.engine.step()\n",
                encoding="utf-8",
            )
            config = {
                "docs": {"class_diagram": True, "sequence_diagram": True},
                "paths": {
                    "source": "src/ai_game_player",
                    "class_diagram": "doc/class_diagram.mmd",
                    "sequence_diagram": "doc/sequence_diagram.mmd",
                },
                "sequence": {
                    "source_file": "src/ai_game_player/pipeline.py",
                    "class_name": "DecisionPipeline",
                    "method_name": "run_and_execute",
                },
            }
            documents = generated_documents(root, config)
            self.assertEqual(stale_documents(documents), [document.path for document in documents])
            write_documents(documents)
            self.assertEqual(stale_documents(documents), [])
            (source / "pipeline.py").write_text(
                "class DecisionPipeline:\n    def run_and_execute(self):\n        self.source.read()\n\n    def retry(self):\n        pass\n",
                encoding="utf-8",
            )
            self.assertEqual(
                stale_documents(generated_documents(root, config)),
                [root / "doc" / "class_diagram.mmd", root / "doc" / "sequence_diagram.mmd"],
            )