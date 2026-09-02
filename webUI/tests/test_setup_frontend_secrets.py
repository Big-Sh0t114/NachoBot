from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class SetupFrontendSecretContractTests(unittest.TestCase):
    def test_frontend_secret_contract(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        fixture = repo_root / "webUI" / "tests" / "frontend_secret_contract.test.js"
        result = subprocess.run(
            ["node", str(fixture)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, "frontend contract fixture failed")


if __name__ == "__main__":
    unittest.main()
