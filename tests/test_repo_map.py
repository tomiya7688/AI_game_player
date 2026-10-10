import json
import tempfile
import unittest
from pathlib import Path

from tools.analyze_repo import RepositoryMapError, RepositoryMapGenerator


class RepositoryMapGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "config").mkdir()
        self.config_path = self.root / "config" / "repo_map.json"
        self.write_config()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_config(self, *, include=None, exclude=None, output="generated/repo_map.json"):
        config = {
            "schema_version": 1,
            "include": include or ["src/**/*.py", "tools/**/*.py", "tests/**/*.py"],
            "exclude": exclude or ["**/__pycache__/**", "**/generated/**", "**/.venv/**"],
            "output": output,
        }
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def write_source_fixture(self):
        package = self.root / "src" / "sample_pkg"
        generated = package / "generated"
        package.mkdir(parents=True)
        generated.mkdir()
        (package / "__init__.py").write_text("from . import models\n", encoding="utf-8")
        (package / "helpers.py").write_text("def convert(value): return value\n", encoding="utf-8")
        (package / "consumer.py").write_text(
            "from sample_pkg.models import Snapshot\n", encoding="utf-8"
        )
        (package / "models.py").write_text(
            "from dataclasses import dataclass\n"
            "from enum import Enum\n"
            "from typing import Protocol\n"
            "from . import helpers\n"
            "@dataclass(frozen=True)\n"
            "class Snapshot:\n"
            "    value: int\n"
            "class Phase(Enum):\n"
            "    READY = 1\n"
            "class Reader(Protocol[str]):\n"
            "    def read(self, key: str) -> str: ...\n"
            "class Calculator:\n"
            "    def compute(self, value: int, /, scale: float = 1.0, *, enabled: bool = True) -> str:\n"
            "        def normalize(text: str = 'private default') -> str:\n"
            "            return text\n"
            "        return normalize(str(value * scale)) if enabled else ''\n"
            "async def load_snapshot(path: str) -> Snapshot:\n"
            "    return Snapshot(0)\n"
            "MODULE_SECRET = 'must not be copied into the map'\n",
            encoding="utf-8",
        )
        (generated / "ignored.py").write_text("class Hidden: pass\n", encoding="utf-8")
        nested_generated = generated / "nested"
        nested_generated.mkdir()
        (nested_generated / "also_ignored.py").write_text("class AlsoHidden: pass\n", encoding="utf-8")
        tests = self.root / "tests" / "fixtures"
        tests.mkdir(parents=True)
        (tests / "ignored.py").write_text("class FixtureOnly: pass\n", encoding="utf-8")
        (self.root / "tools").mkdir()
        (self.root / "tools" / "helper.py").write_text("def helper(value): return value\n", encoding="utf-8")

    def test_map_contains_modules_symbols_imports_and_static_data_model_metadata(self):
        self.write_source_fixture()
        generator = RepositoryMapGenerator(self.root, self.config_path)
        repository_map = generator.build()
        modules_by_name = {module["module"]: module for module in repository_map["modules"]}
        sample_package = modules_by_name["sample_pkg"]
        sample_models = modules_by_name["sample_pkg.models"]
        symbols_by_name = {symbol["qualified_name"]: symbol for symbol in sample_models["symbols"]}

        self.assertEqual(1, repository_map["schema_version"])
        self.assertEqual(["sample_pkg.models"], sample_package["dependencies"])
        self.assertEqual(["sample_pkg.helpers"], sample_models["dependencies"])
        self.assertEqual(["sample_pkg.models"], modules_by_name["sample_pkg.consumer"]["dependencies"])
        self.assertEqual("class", symbols_by_name["Snapshot"]["kind"])
        self.assertEqual({"dataclass": True, "enum": False, "protocol": False}, symbols_by_name["Snapshot"]["data_model"])
        self.assertTrue(symbols_by_name["Phase"]["data_model"]["enum"])
        self.assertTrue(symbols_by_name["Reader"]["data_model"]["protocol"])
        self.assertEqual("async_function", symbols_by_name["load_snapshot"]["kind"])
        self.assertEqual("(path: str) -> Snapshot", symbols_by_name["load_snapshot"]["signature"])

        compute = symbols_by_name["Calculator.compute"]
        self.assertEqual("method", compute["kind"])
        self.assertEqual("(self, value: int, /, scale: float = …, *, enabled: bool = …) -> str", compute["signature"])
        self.assertEqual(
            ["positional_only", "positional_only", "positional_or_keyword", "keyword_only"],
            [parameter["kind"] for parameter in compute["parameters"]],
        )
        self.assertEqual("function", symbols_by_name["Calculator.compute.normalize"]["kind"])
        self.assertNotIn("normalize", symbols_by_name)
        self.assertEqual(6, symbols_by_name["Snapshot"]["source_location"]["start_line"])
        self.assertNotIn("must not be copied", json.dumps(repository_map, ensure_ascii=False))
        self.assertNotIn("ignored", modules_by_name)
        self.assertNotIn("sample_pkg.generated.nested.also_ignored", modules_by_name)
        self.assertIn("tools.helper", modules_by_name)

    def test_write_is_deterministic_and_check_does_not_rewrite_stale_output(self):
        self.write_source_fixture()
        generator = RepositoryMapGenerator(self.root, self.config_path)
        self.assertTrue(generator.write())
        output = self.root / "generated" / "repo_map.json"
        first_content = output.read_text(encoding="utf-8")
        self.assertTrue(generator.write())
        self.assertEqual(first_content, output.read_text(encoding="utf-8"))
        self.assertTrue(generator.write(check=True))

        output.write_text("stale output", encoding="utf-8")
        self.assertFalse(generator.write(check=True))
        self.assertEqual("stale output", output.read_text(encoding="utf-8"))

    def test_parse_failure_preserves_the_previous_repository_map(self):
        self.write_source_fixture()
        generator = RepositoryMapGenerator(self.root, self.config_path)
        self.assertTrue(generator.write())
        output = self.root / "generated" / "repo_map.json"
        previous_content = output.read_text(encoding="utf-8")
        (self.root / "src" / "sample_pkg" / "models.py").write_text("def incomplete(:\n", encoding="utf-8")

        with self.assertRaisesRegex(RepositoryMapError, r"models\.py:1:"):
            generator.write()

        self.assertEqual(previous_content, output.read_text(encoding="utf-8"))

    def test_config_rejects_parent_traversal_and_unknown_keys(self):
        self.write_config(include=["../outside/**/*.py"])
        with self.assertRaisesRegex(RepositoryMapError, "relative POSIX path"):
            RepositoryMapGenerator(self.root, self.config_path)

        self.write_config()
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["schema_version"] = True
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(RepositoryMapError, "schema_version must be 1"):
            RepositoryMapGenerator(self.root, self.config_path)

        config["schema_version"] = 1
        config["unexpected"] = True
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(RepositoryMapError, "keys are invalid"):
            RepositoryMapGenerator(self.root, self.config_path)

    def test_duplicate_module_names_fail_instead_of_creating_ambiguous_records(self):
        self.write_source_fixture()
        self.write_config(include=["src/**/*.py", "*.py"])
        (self.root / "sample_pkg.py").write_text("class DuplicatePackage: pass\n", encoding="utf-8")

        with self.assertRaisesRegex(RepositoryMapError, "is ambiguous"):
            RepositoryMapGenerator(self.root, self.config_path).build()

    def test_source_symlink_outside_repository_is_rejected(self):
        self.write_source_fixture()
        source_directory = self.root / "src" / "sample_pkg"
        outside_source = self.root.parent / "repo_map_external_source.py"
        outside_source.write_text("class External: pass\n", encoding="utf-8")
        try:
            (source_directory / "external.py").symlink_to(outside_source)
        except (OSError, NotImplementedError) as error:
            outside_source.unlink(missing_ok=True)
            self.skipTest(f"the platform cannot create a source symlink: {error}")
        try:
            with self.assertRaisesRegex(RepositoryMapError, "symlink escapes"):
                RepositoryMapGenerator(self.root, self.config_path).build()
        finally:
            outside_source.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
