from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools" / "runtime_boundary_check.py"
MANIFEST = ROOT / "config" / "runtime_boundary.json"


class RuntimeBoundaryCheckTest(unittest.TestCase):
    def test_repository_runtime_boundary_is_consistent(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(CHECKER)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("RUNTIME BOUNDARY OK", completed.stdout)

    def test_contract_version_drift_is_rejected(self) -> None:
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        value["contract_version"] = 999
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime_boundary.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(CHECKER), "--manifest", str(path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("contract version mismatch", completed.stderr)


if __name__ == "__main__":
    unittest.main()
