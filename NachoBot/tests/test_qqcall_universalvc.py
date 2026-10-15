from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class QQCallUniversalVCBoundaryTests(unittest.TestCase):
    def test_control_and_public_sdk_imports_do_not_load_native_audio_or_ml(self) -> None:
        env = dict(os.environ)
        prior = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + prior if prior else "")
        check = r"""
import importlib
import sys
for name in (
    'qqcall_client.qqcall',
    'qqcall_client.qqcall_voice',
    'qqcall_client.qqcall_universal_api',
    'universalvc_client',
):
    importlib.import_module(name)
forbidden = ('numpy', 'onnxruntime', 'sherpa_onnx', 'sounddevice', 'pyaudio', 'qqcall_pcm', 'universalvc_service')
loaded = sorted(name for name in sys.modules if any(name == prefix or name.startswith(prefix + '.') for prefix in forbidden))
assert not loaded, loaded
"""
        result = subprocess.run(
            [sys.executable, "-c", check],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
