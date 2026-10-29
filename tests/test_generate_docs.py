import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from tools.generate_docs import generated_documents, stale_documents, write_documents


class GenerateDocsTest(unittest.TestCase):
    def test_generates_and_detects_stale_diagrams(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "ai_game_player"
            source.mkdir(parents=True)
            repository_config = root / "config" / "repo_map.json"
            repository_config.parent.mkdir()
            repository_config.write_text(
                '{"schema_version":1,"include":["src/**/*.py"],"exclude":[],"output":"generated/repo_map.json"}',
                encoding="utf-8",
            )
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
                "repository_map": {"config": "config/repo_map.json"},
            }
            documents = generated_documents(root, config)
            self.assertEqual(stale_documents(documents), [document.path for document in documents])
            repository_map_document = next(document for document in documents if document.path.name == "repo_map.json")
            self.assertIsNotNone(repository_map_document.publisher)
            with patch.object(Path, "write_text", side_effect=AssertionError("repository map must publish atomically")):
                write_documents([repository_map_document])
            self.assertEqual(stale_documents([repository_map_document]), [])
            write_documents(documents)
            self.assertEqual(stale_documents(documents), [])
            self.assertTrue((root / "generated" / "repo_map.json").is_file())
            (source / "pipeline.py").write_text(
                "class DecisionPipeline:\n    def run_and_execute(self):\n        self.source.read()\n\n    def retry(self):\n        pass\n",
                encoding="utf-8",
            )
            self.assertEqual(
                stale_documents(generated_documents(root, config)),
                [
                    root / "doc" / "class_diagram.mmd",
                    root / "doc" / "sequence_diagram.mmd",
                    root / "generated" / "repo_map.json",
                ],
            )
